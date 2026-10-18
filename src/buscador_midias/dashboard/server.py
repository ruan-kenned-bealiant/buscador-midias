"""Servidor local do mapa e do painel operacional, com APIs somente leitura."""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg

from buscador_midias.dashboard.mapping import mapping_catalog, mapping_mechanisms, mapping_sections
from buscador_midias.dashboard.snapshot import DEFAULT_HOURS, DEFAULT_RECENT, build_snapshot
from buscador_midias.sites.loader import load_sites
from buscador_midias.state.store import StateConfigError, StateSchemaError, StateStore
from buscador_midias.strategy.manual import load_manual_rules
from buscador_midias.strategy.mechanism_inventory import MechanismIndex

DEFAULT_CACHE_SECONDS = 4.0
_MAX_HOURS = 168
_MAX_RECENT = 200
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
)
_LOG = logging.getLogger(__name__)


def make_server(
    open_state: Callable[[], StateStore],
    host: str,
    port: int,
    *,
    clock: Callable[[], datetime] | None = None,
    schedule_path: Path | None = None,
    cache_seconds: float = DEFAULT_CACHE_SECONDS,
    sites_path: Path | None = None,
) -> ThreadingHTTPServer:
    """Cria o servidor do painel (sem iniciá-lo).

    O estado é aberto somente leitura a cada cálculo do retrato e fechado em seguida, então o painel
    nunca grava nem segura o banco enquanto a descoberta e o enriquecimento trabalham.

    Args:
        open_state: Abre o estado somente leitura (uma conexão por cálculo do retrato).
        host: Endereço de escuta (use ``127.0.0.1``: o painel não tem autenticação).
        port: Porta (``0`` escolhe uma livre).
        clock: Relógio (injetável); padrão é o horário atual em UTC.
        schedule_path: JSON com as execuções previstas, relido a cada requisição (opcional).
        cache_seconds: Por quanto tempo o retrato calculado é reaproveitado entre as requisições
            (várias abas ou atualizações seguidas não recalculam tudo).
        sites_path: Catálogo CSV do mapa; padrão é o catálogo versionado do projeto.

    Returns:
        O servidor, pronto para ``serve_forever``.
    """
    now_fn = clock or (lambda: datetime.now(UTC))
    cache: dict[tuple[int, int], tuple[datetime, bytes]] = {}
    cache_lock = threading.Lock()
    page = resources.files("buscador_midias.dashboard").joinpath("index.html").read_bytes()
    map_page = resources.files("buscador_midias.dashboard").joinpath("mapping.html").read_bytes()
    catalog = load_sites(sites_path or Path(__file__).resolve().parents[3] / "data/sites.csv")
    sites = {s.key: s for s in catalog.sites}
    map_cache: tuple[datetime, bytes] | None = None
    map_lock = threading.Lock()
    rules = load_manual_rules(Path(__file__).resolve().parents[3] / "data/estrategias_manuais.csv")
    indexes: dict[str, tuple[datetime, MechanismIndex]] = {}

    class Handler(BaseHTTPRequestHandler):
        """Atende o mapa, suas consultas e as rotas do painel operacional anterior."""

        server_version = "BuscadorPainel"

        def do_GET(self) -> None:  # noqa: N802 - nome exigido pelo http.server
            """Roteia as requisições GET."""
            parts = urlsplit(self.path)
            if parts.path in ("/", "/mapa"):
                self._send(200, "text/html; charset=utf-8", map_page)
            elif parts.path == "/fluxo":
                self._send(200, "text/html; charset=utf-8", page)
            elif parts.path in (
                "/api/mapa/fontes",
                "/api/mapa/secoes",
                "/api/mapa/mecanismos",
                "/api/mapa/mecanismos.csv",
            ):
                self._mapping(parts.path, parse_qs(parts.query))
            elif parts.path == "/api/snapshot":
                self._snapshot(parse_qs(parts.query))
            else:
                self._json(404, {"error": "não encontrado"})

        def _mapping(self, path: str, query: dict[str, list[str]]) -> None:
            """Consulta o catálogo ou uma página de seções, sem iniciar buscas."""
            nonlocal map_cache
            key = query.get("site", [""])[0]
            mode = query.get("mode", ["examples"])[0]
            if path.endswith("/secoes"):
                if key not in sites:
                    self._json(404, {"error": "fonte não encontrada"})
                    return
                if mode not in ("examples", "read", "gaps", "all"):
                    self._json(400, {"error": "filtro de seções inválido"})
                    return
            mechanisms = path.endswith(("/mecanismos", "/mecanismos.csv"))
            if mechanisms:
                if key and key not in sites:
                    self._json(404, {"error": "fonte não encontrada"})
                    return
                mode = query.get("mode", ["recent"])[0]
                if mode not in ("recent", "supplement", "review", "all"):
                    self._json(400, {"error": "filtro de mecanismos inválido"})
                    return
            with map_lock:
                now = now_fn()
                if (
                    path.endswith("/fontes")
                    and map_cache is not None
                    and (now - map_cache[0]).total_seconds() < 30
                ):
                    self._send(200, "application/json; charset=utf-8", map_cache[1])
                    return
                try:
                    store = open_state()
                    try:
                        if path.endswith("/fontes"):
                            data = mapping_catalog(store, catalog, rules)
                        elif mechanisms:
                            records = mapping_mechanisms(
                                store,
                                [sites[key]] if key else catalog.sites,
                                rules=rules,
                                mode=mode,
                                search=query.get("q", [""])[0][:200],
                            )
                            if path.endswith(".csv"):
                                output = io.StringIO(newline="")
                                writer = csv.writer(output)
                                writer.writerow(
                                    (
                                        "dominio",
                                        "news_sitemap_url",
                                        "rss_url",
                                        "sitemap_url",
                                        "resultado",
                                        "validado_em",
                                        "leitura_parcial",
                                        "grupo",
                                    )
                                )
                                for record in records:
                                    columns = [record["site_key"], "", "", ""]
                                    columns[
                                        {"news_sitemap": 1, "feed": 2, "sitemap": 3}[record["kind"]]
                                    ] = record["url"]
                                    writer.writerow(
                                        columns
                                        + [
                                            record["verdict"],
                                            record["checked_at"],
                                            record["partial"],
                                            record["group"],
                                        ]
                                    )
                                self._send(
                                    200,
                                    "text/csv; charset=utf-8",
                                    output.getvalue().encode("utf-8-sig"),
                                )
                                return
                            try:
                                offset = max(0, int(query.get("offset", ["0"])[0]))
                            except ValueError:
                                offset = 0
                            limit = _bounded(query, "limit", 50, 200)
                            data = {
                                "total": len(records),
                                "offset": offset,
                                "mechanisms": records[offset : offset + limit],
                            }
                        else:
                            cached_index = indexes.get(key)
                            if (
                                cached_index is None
                                or (now - cached_index[0]).total_seconds() >= 60
                            ):
                                if len(indexes) >= 3:
                                    indexes.pop(next(iter(indexes)))
                                cached_index = (
                                    now,
                                    MechanismIndex(
                                        sites[key],
                                        store.mapping_sources(key),
                                        rules.for_site(sites[key]).exclude,
                                    ),
                                )
                                indexes[key] = cached_index
                            try:
                                offset = min(1_000_000, max(0, int(query.get("offset", ["0"])[0])))
                            except ValueError:
                                offset = 0
                            data = mapping_sections(
                                store,
                                sites[key],
                                mode=mode,
                                search=query.get("q", [""])[0][:200],
                                offset=offset,
                                limit=_bounded(query, "limit", 50, 200),
                                index=cached_index[1],
                            )
                    finally:
                        store.close()
                except (psycopg.Error, StateConfigError, StateSchemaError):
                    _LOG.exception("falha ao consultar o mapa")
                    self._json(503, {"error": "mapa indisponível"})
                    return
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                if path.endswith("/fontes"):
                    map_cache = (now, body)
                self._send(200, "application/json; charset=utf-8", body)

        def _snapshot(self, query: dict[str, list[str]]) -> None:
            """Responde o retrato do estado (do cache, se recente); falha de leitura vira 503."""
            hours = _bounded(query, "hours", DEFAULT_HOURS, _MAX_HOURS)
            recent = _bounded(query, "recent", DEFAULT_RECENT, _MAX_RECENT)
            key = (hours, recent)
            body: bytes | None
            with cache_lock:
                now = now_fn()
                cached = cache.get(key)
                if cached is not None and (now - cached[0]).total_seconds() < cache_seconds:
                    body = cached[1]
                else:
                    body = self._compute(now, hours, recent)
                    if body is not None:
                        cache[key] = (now, body)
            if body is None:
                self._json(503, {"error": "estado indisponível"})
                return
            self._send(200, "application/json; charset=utf-8", body)

        def _compute(self, now: datetime, hours: int, recent: int) -> bytes | None:
            """Calcula o retrato e o serializa; ``None`` se o estado não pôde ser lido."""
            try:
                store = open_state()
            except (psycopg.Error, StateConfigError, StateSchemaError):
                _LOG.exception("falha ao abrir o estado")
                return None
            try:
                snapshot = build_snapshot(
                    store,
                    now=now,
                    hours=hours,
                    recent=recent,
                    schedule_path=schedule_path,
                )
            except psycopg.Error:
                _LOG.exception("falha ao ler o estado")
                return None
            finally:
                store.close()
            return json.dumps(snapshot, ensure_ascii=False).encode("utf-8")

        def _json(self, status: int, payload: dict[str, object]) -> None:
            """Responde com JSON."""
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, "application/json; charset=utf-8", body)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            """Envia a resposta com cabeçalhos de segurança e sem cache."""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP)
            self.end_headers()
            self.wfile.write(body)

        def _not_allowed(self) -> None:
            """Recusa métodos que não sejam GET."""
            body = json.dumps({"error": "método não permitido"}).encode("utf-8")
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_PUT = do_DELETE = do_PATCH = _not_allowed  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Silencia o log por requisição (o painel consulta a cada poucos segundos)."""

    return ThreadingHTTPServer((host, port), Handler)


def _bounded(query: dict[str, list[str]], name: str, default: int, maximum: int) -> int:
    """Parâmetro inteiro de consulta entre 1 e ``maximum``; inválido cai no padrão."""
    try:
        value = int(query[name][0])
    except (KeyError, IndexError, ValueError):
        return default
    return min(max(value, 1), maximum)
