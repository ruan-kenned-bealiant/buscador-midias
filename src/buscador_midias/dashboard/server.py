"""Servidor local do gerenciador e das consultas históricas do buscador."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg

from buscador_midias.catalog.storage import open_catalog
from buscador_midias.catalog.store import CatalogError
from buscador_midias.catalog.testing import ReaderFactory
from buscador_midias.dashboard.access_audit import AccessAudit
from buscador_midias.dashboard.catalog_api import CatalogAPI
from buscador_midias.dashboard.functional import FunctionalView
from buscador_midias.dashboard.guide import render_guide
from buscador_midias.dashboard.mapping import mapping_catalog, mapping_mechanisms, mapping_sections
from buscador_midias.dashboard.round_view import round_links, round_summary
from buscador_midias.dashboard.selection import CATEGORIES, DOWNLOADS, SelectionView
from buscador_midias.dashboard.snapshot import DEFAULT_HOURS, DEFAULT_RECENT, build_snapshot
from buscador_midias.sites.loader import load_sites
from buscador_midias.state.store import StateConfigError, StateSchemaError, StateStore
from buscador_midias.strategy.manual import load_manual_rules
from buscador_midias.strategy.mechanism_inventory import MechanismIndex

DEFAULT_CACHE_SECONDS = 4.0
_MAX_HOURS = 168
_MAX_RECENT = 200
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
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
    audit_directory: Path | None = None,
    round_directory: Path | None = None,
    selection_directory: Path | None = None,
    functional_directory: Path | None = None,
    manager_db_path: Path | None = None,
    test_reader_factory: ReaderFactory | None = None,
    priority_probe_directory: Path | None = None,
    feeder_mapping_directory: Path | None = None,
    feeder_pending_directory: Path | None = None,
    coverage_directory: Path | None = None,
) -> ThreadingHTTPServer:
    """Cria o servidor do painel (sem iniciá-lo).

    O estado da coleta é aberto somente leitura a cada cálculo do retrato e fechado em seguida.
    Escritas do gerenciador usam um cadastro independente e preservam os relatórios históricos.

    Args:
        open_state: Abre o estado somente leitura (uma conexão por cálculo do retrato).
        host: Endereço de escuta (use ``127.0.0.1``: o painel não tem autenticação).
        port: Porta (``0`` escolhe uma livre).
        clock: Relógio (injetável); padrão é o horário atual em UTC.
        schedule_path: JSON com as execuções previstas, relido a cada requisição (opcional).
        cache_seconds: Por quanto tempo o retrato calculado é reaproveitado entre as requisições
            (várias abas ou atualizações seguidas não recalculam tudo).
        sites_path: Catálogo CSV do mapa; padrão é o catálogo versionado do projeto.
        audit_directory: Pasta dos relatórios de acesso; padrão é ``var`` do projeto.
        round_directory: Configuração e progresso da rodada 12; padrão é a pasta da execução.
        selection_directory: Pacote estático dos planos e decisões da etapa 5.
        functional_directory: Retrato offline dos RSS/sitemaps funcionais por fonte.
        manager_db_path: Cadastro editável separado, em SQLite local.
        test_reader_factory: Leitor dos testes interativos, substituível em testes sem rede.
        priority_probe_directory: Pasta local do teste finito de notícias e evidências.
        feeder_mapping_directory: Artefatos da descoberta complementar via Feeder.
        feeder_pending_directory: Artefatos da conclusão dos candidatos RSS pendentes.
        coverage_directory: Referências e comparação da cobertura dos veículos prioritários.

    Returns:
        O servidor, pronto para ``serve_forever``.
    """
    priority_probe = priority_probe_directory or (
        Path(__file__).resolve().parents[3] / "var/gerenciador/teste-noticias-evidencias-20261026"
    )
    feeder_mapping = feeder_mapping_directory or (
        Path(__file__).resolve().parents[3] / "var/gerenciador/mapeamento-feeder-20261026"
    )
    feeder_pending = feeder_pending_directory or (
        Path(__file__).resolve().parents[3] / "var/gerenciador/feeder-pendentes-20261026"
    )
    audit_reports = {
        "/testes/cobertura": coverage_directory
        or Path(__file__).resolve().parents[3] / "var/gerenciador/cobertura-capas-3727178f",
        "/testes/prioritarios": priority_probe,
        "/testes/feeder": feeder_mapping,
        "/testes/feeder-pendentes": feeder_pending,
    }
    audit_evidence = {
        "teste-prioritarios": priority_probe,
        "feeder": feeder_mapping,
        "feeder-pendentes": feeder_pending,
    }
    now_fn = clock or (lambda: datetime.now(UTC))
    cache: dict[tuple[int, int], tuple[datetime, bytes]] = {}
    cache_lock = threading.Lock()
    selection = SelectionView(
        selection_directory
        or (Path(__file__).resolve().parents[3] / "var/analise-rodada-12/etapa-5")
    )
    functional = FunctionalView(
        functional_directory or (Path(__file__).resolve().parents[3] / "var/consulta-funcionais")
    )
    manager_page = (
        resources.files("buscador_midias.dashboard").joinpath("manager.html").read_bytes()
    )
    guide_page = render_guide()
    search_page = resources.files("buscador_midias.dashboard").joinpath("search.html").read_bytes()
    search_script = resources.files("buscador_midias.dashboard").joinpath("search.js").read_bytes()
    evidence_assets = {
        name: resources.files("buscador_midias.dashboard").joinpath(name).read_bytes()
        for name in ("evidence.html", "evidence.js", "evidence_prepare.js")
    }
    health_script = resources.files("buscador_midias.dashboard").joinpath("health.js").read_bytes()
    manager = CatalogAPI(
        open_catalog(
            manager_db_path
            or Path(__file__).resolve().parents[3] / "var/gerenciador/catalogo.sqlite",
            functional.directory / "retrato.json",
            sites_path or Path(__file__).resolve().parents[3] / "data/sites.csv",
            explicit_sqlite=manager_db_path is not None,
        )
    )
    if test_reader_factory is not None:
        manager.jobs.factory = test_reader_factory
        if manager.health_worker:
            manager.health_worker.factory = test_reader_factory
    round_dir = round_directory or (
        Path(__file__).resolve().parents[3] / "var/ciclo-busca-20261019T005450Z"
    )
    round_cache: tuple[datetime, dict[str, object]] | None = None
    round_lock = threading.Lock()
    catalog = load_sites(sites_path or Path(__file__).resolve().parents[3] / "data/sites.csv")
    sites = {s.key: s for s in catalog.sites}
    map_cache: tuple[datetime, bytes] | None = None
    map_lock = threading.Lock()
    rules = load_manual_rules(Path(__file__).resolve().parents[3] / "data/estrategias_manuais.csv")
    audit = AccessAudit(
        audit_directory or Path(__file__).resolve().parents[3] / "var", list(catalog.sites), rules
    )
    indexes: dict[str, tuple[datetime, MechanismIndex]] = {}

    class Handler(BaseHTTPRequestHandler):
        """Atende o mapa, suas consultas e as rotas do painel operacional anterior."""

        server_version = "BuscadorPainel"

        def do_GET(self) -> None:  # noqa: N802 - nome exigido pelo http.server
            """Roteia as requisições GET."""
            parts = urlsplit(self.path)
            if parts.path in ("/", "/gerenciador", "/mecanismos", "/saude"):
                self._send(200, "text/html; charset=utf-8", manager_page)
            elif parts.path in ("/assets/evidence.js", "/assets/evidence_prepare.js"):
                self._send(
                    200,
                    "application/javascript; charset=utf-8",
                    evidence_assets[parts.path.rsplit("/", 1)[-1]],
                )
            elif parts.path == "/buscador":
                self._send(200, "text/html; charset=utf-8", search_page)
            elif parts.path == "/assets/search.js":
                self._send(200, "application/javascript; charset=utf-8", search_script)
            elif parts.path == "/guia":
                self._send(200, "text/html; charset=utf-8", guide_page)
            elif parts.path == "/assets/health.js":
                self._send(200, "application/javascript; charset=utf-8", health_script)
            elif parts.path in audit_reports:
                try:
                    report_root = audit_reports[parts.path]
                    data = (report_root / "report.html").read_bytes()
                    self._send(200, "text/html; charset=utf-8", data)
                except OSError:
                    self._json(404, {"error": "Relatório de teste ainda não disponível."})
            elif parts.path.startswith(
                (
                    "/evidencias/corpus/",
                    "/evidencias/teste-prioritarios/",
                    "/evidencias/feeder/",
                    "/evidencias/feeder-pendentes/",
                )
            ):
                from buscador_midias.ingestion.evidence_view import read_evidence, read_local

                match = re.fullmatch(
                    r"/evidencias/(corpus|teste-prioritarios|feeder|feeder-pendentes)/([a-f0-9]{64})",
                    parts.path,
                )
                if not match or (match[1] == "corpus" and manager.ingestion is None):
                    self._json(404, {"error": "Evidência não encontrada."})
                    return
                try:
                    if match[1] != "corpus":
                        content_type = "text/html"
                        data = read_local(
                            f"midias-ia/evidencias/{match[2]}.html",
                            [audit_evidence[match[1]] / "evidence"],
                        )
                    else:
                        content_type, data = read_evidence(manager.ingestion, match[2])
                    original = parse_qs(parts.query).get("original") == ["1"]
                    if content_type == "text/html" and not original:
                        self._send(
                            200,
                            "text/html; charset=utf-8",
                            evidence_assets["evidence.html"],
                            csp="default-src 'none'; script-src 'self'; "
                            "style-src 'unsafe-inline' http: https:; connect-src 'self'; "
                            "frame-src 'self'; img-src http: https: data:; "
                            "font-src http: https: data:; base-uri http: https:; "
                            "form-action 'none'",
                        )
                        return
                    self._send(
                        200,
                        content_type,
                        data,
                        csp="sandbox; script-src 'none'; object-src 'none'; form-action 'none'",
                        download=original,
                    )
                except CatalogError as error:
                    self._json(error.status, {"error": str(error)})
                except (OSError, psycopg.Error, ValueError):
                    self._json(503, {"error": "Evidência indisponível. Tente novamente."})
            elif parts.path.startswith("/api/gerenciador/"):
                self._json(*manager.handle(self))
            elif parts.path in (
                "/mapa",
                "/fluxo",
                "/rodada-12",
                "/selecao",
                "/funcionais",
                "/classificacao",
            ):
                self._json(410, {"error": "Tela removida. Acesse o gerenciador em /."})
            elif parts.path in (
                "/api/funcionais",
                "/api/funcionais/mecanismos",
                "/api/funcionais/arquivo",
            ):
                self._functional(parts.path, parse_qs(parts.query))
            elif parts.path in ("/api/selecao", "/api/selecao/mecanismos", "/api/selecao/arquivo"):
                self._selection(parts.path, parse_qs(parts.query))
            elif parts.path in ("/api/rodada-12", "/api/rodada-12/noticias"):
                self._round(parts.path, parse_qs(parts.query))
            elif parts.path == "/api/mapa/acesso":
                self._access(parse_qs(parts.query))
            elif parts.path == "/api/mapa/validacao-secoes":
                self._validation_sections(parse_qs(parts.query))
            elif parts.path == "/api/mapa/revisao-web":
                self._web_review(parse_qs(parts.query))
            elif parts.path == "/api/mapa/plano-validado.json":
                mode = parse_qs(parts.query).get("mode", ["news"])[0]
                names = {"news": "plano-com-noticias.json", "xml": "plano-xml-validado.json"}
                if mode not in names:
                    self._json(400, {"error": "tipo de plano inválido"})
                    return
                report = audit.directory / "validacao-final" / names[mode]
                if report.is_file():
                    self._send(200, "application/json; charset=utf-8", report.read_bytes())
                else:
                    self._json(404, {"error": "plano validado ainda não disponível"})
            elif parts.path == "/api/mapa/auditoria.xlsx":
                report = audit.directory / "validacao-final/resultado.xlsx"
                if not report.is_file():
                    report = audit.directory / "revisao-web-fontes/resultado.xlsx"
                if not report.is_file():
                    report = audit.directory / "exploracao-pendentes-final/resultado.xlsx"
                if not report.is_file():
                    report = audit.directory / "auditoria-acesso-rss/resultado.xlsx"
                if report.is_file():
                    self._send(
                        200,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        report.read_bytes(),
                    )
                else:
                    self._json(404, {"error": "planilha de auditoria não disponível"})
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

        def _functional(self, path: str, query: dict[str, list[str]]) -> None:
            """Consulta arquivos derivados; não modifica o histórico nem abre o banco."""
            downloads = {
                "fontes.csv": "text/csv; charset=utf-8",
                "funcionais.csv": "text/csv; charset=utf-8",
                "links-com-texto.csv": "text/csv; charset=utf-8",
                "fontes-funcionais.xlsx": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                "historico-preservado.json": "application/json; charset=utf-8",
                "auditoria-dirigida.md": "text/markdown; charset=utf-8",
                "diagnostico-fontes.csv": "text/csv; charset=utf-8",
                "diagnostico-links.csv": "text/csv; charset=utf-8",
                "conclusao.json": "application/json; charset=utf-8",
                "auditoria-final.xlsx": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
            }
            try:
                if path.endswith("/arquivo"):
                    name = query.get("nome", [""])[0]
                    if name not in downloads:
                        self._json(400, {"error": "arquivo não permitido"})
                        return
                    self._send(200, downloads[name], (functional.directory / name).read_bytes())
                elif path.endswith("/mecanismos"):
                    kind = query.get("tipo", ["all"])[0]
                    usage = query.get("uso", ["all"])[0]
                    if usage not in ("all", "text", "xml", "audio"):
                        self._json(400, {"error": "uso inválido"})
                        return
                    offset = int(query.get("offset", ["0"])[0])
                    limit = int(query.get("limit", ["25"])[0])
                    if kind not in ("all", "feed", "sitemap"):
                        self._json(400, {"error": "tipo inválido"})
                        return
                    if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
                        self._json(400, {"error": "paginação inválida"})
                        return
                    self._json(
                        200,
                        functional.mechanisms(
                            query.get("site", [""])[0],
                            kind,
                            query.get("q", [""])[0][:200],
                            offset,
                            limit,
                            usage=usage,
                        ),
                    )
                else:
                    self._json(200, functional.summary())
            except KeyError:
                self._json(404, {"error": "fonte não encontrada"})
            except ValueError:
                self._json(400, {"error": "parâmetro inválido"})
            except OSError:
                self._json(503, {"error": "retrato funcional indisponível; tente novamente"})

        def _selection(self, path: str, query: dict[str, list[str]]) -> None:
            """Serve somente os relatórios e arquivos permitidos, sem abrir o banco."""
            try:
                if path.endswith("/arquivo"):
                    name = query.get("nome", [""])[0]
                    if name not in DOWNLOADS:
                        self._json(400, {"error": "arquivo não permitido"})
                        return
                    self._send(200, DOWNLOADS[name], (selection.directory / name).read_bytes())
                elif path.endswith("/mecanismos"):
                    category = query.get("categoria", ["all"])[0]
                    if category not in CATEGORIES:
                        self._json(400, {"error": "classificação inválida"})
                        return
                    try:
                        offset = int(query.get("offset", ["0"])[0])
                        limit = int(query.get("limit", ["25"])[0])
                        if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
                            raise ValueError("paginação inválida")
                    except ValueError:
                        self._json(400, {"error": "paginação inválida"})
                        return
                    self._json(
                        200,
                        selection.mechanisms(
                            query.get("site", [""])[0],
                            category,
                            query.get("q", [""])[0][:200],
                            offset,
                            limit,
                        ),
                    )
                else:
                    self._json(200, selection.summary())
            except KeyError:
                self._json(404, {"error": "fonte não encontrada"})
            except (OSError, ValueError, csv.Error):
                _LOG.exception("falha ao ler seleção local")
                self._json(503, {"error": "entrega da seleção indisponível; tente novamente"})

        def _round(self, path: str, query: dict[str, list[str]]) -> None:
            """Consulta exclusiva da rodada 12, com leitura paginada de notícias."""
            nonlocal round_cache
            key = query.get("site", [""])[0]
            if key and key not in sites:
                self._json(404, {"error": "fonte não encontrada"})
                return
            try:
                raw_cursor = query.get("before", [""])[0]
                cursor = int(raw_cursor) if raw_cursor else None
                if cursor is not None and cursor < 1:
                    raise ValueError("cursor inválido")
            except ValueError:
                self._json(400, {"error": "cursor inválido"})
                return
            try:
                with round_lock:
                    now = now_fn()
                    if (
                        path == "/api/rodada-12"
                        and round_cache is not None
                        and (now - round_cache[0]).total_seconds() < cache_seconds
                    ):
                        self._json(200, round_cache[1])
                        return
                    state = open_state()
                    try:
                        if path.endswith("/noticias"):
                            result = round_links(state, round_dir, site=key, before=cursor)
                        else:
                            summary = round_summary(state, sites, round_dir, now)
                            if summary is None:
                                self._json(404, {"error": "rodada 12 ainda não disponível"})
                                return
                            result = summary
                            round_cache = (now, result)
                    finally:
                        state.close()
                self._json(200, result)
            except (psycopg.Error, StateConfigError, StateSchemaError, OSError, ValueError):
                _LOG.exception("falha ao consultar rodada 12")
                self._json(503, {"error": "dados da rodada indisponíveis; tente novamente"})

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
                mode = query.get("mode", ["functional"])[0]
                if mode not in (
                    "functional",
                    "fixed_date",
                    "recent",
                    "supplement",
                    "review",
                    "all",
                ):
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
                            audit.refresh()
                            data["access_audit"] = audit.summary
                            for source in data["sites"]:
                                source["access_audit"] = audit.sources.get(source["key"], {})
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
                                        "xml_funcional",
                                        "data_fixa",
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
                                            record["functional"],
                                            record["fixed_date"],
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
                except (psycopg.Error, StateConfigError, StateSchemaError, OSError, ValueError):
                    _LOG.exception("falha ao consultar o mapa")
                    self._json(503, {"error": "mapa indisponível"})
                    return
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                if path.endswith("/fontes"):
                    map_cache = (now, body)
                self._send(200, "application/json; charset=utf-8", body)

        def _access(self, query: dict[str, list[str]]) -> None:
            """Consulta os relatórios locais; não abre o banco nem acessa os sites."""
            key = query.get("site", [""])[0]
            if key not in sites:
                self._json(404, {"error": "fonte não encontrada"})
                return
            mode = query.get("mode", ["all"])[0]
            kind = query.get("kind", ["all"])[0]
            if mode not in (
                "all",
                "accessible",
                "readable",
                "unconfirmed",
                "untested",
                "partial",
            ) or kind not in ("all", "feed", "sitemap"):
                self._json(400, {"error": "filtro de auditoria inválido"})
                return
            try:
                offset = min(1_000_000, max(0, int(query.get("offset", ["0"])[0])))
            except ValueError:
                offset = 0
            with map_lock:
                try:
                    audit.refresh()
                    data = audit.page(
                        key,
                        kind=kind,
                        mode=mode,
                        search=query.get("q", [""])[0][:200],
                        offset=offset,
                        limit=_bounded(query, "limit", 50, 200),
                    )
                except (OSError, ValueError, csv.Error):
                    _LOG.exception("falha ao ler relatórios de acesso")
                    self._json(503, {"error": "relatórios de auditoria indisponíveis"})
                    return
            self._json(200, data)

        def _validation_sections(self, query: dict[str, list[str]]) -> None:
            """Consulta as evidências editoriais locais da validação final."""
            key = query.get("site", [""])[0]
            if key not in sites:
                self._json(404, {"error": "fonte não encontrada"})
                return
            mode = query.get("mode", ["all"])[0]
            if mode not in ("all", "confirmed", "unconfirmed"):
                self._json(400, {"error": "filtro de validação inválido"})
                return
            try:
                offset = min(1_000_000, max(0, int(query.get("offset", ["0"])[0])))
            except ValueError:
                offset = 0
            with map_lock:
                try:
                    audit.refresh()
                    data = audit.sections_page(
                        key,
                        mode=mode,
                        search=query.get("q", [""])[0][:200],
                        offset=offset,
                        limit=_bounded(query, "limit", 50, 200),
                    )
                except (OSError, ValueError, csv.Error):
                    _LOG.exception("falha ao ler validação por seção")
                    self._json(503, {"error": "validação por seção indisponível"})
                    return
            self._json(200, data)

        def _web_review(self, query: dict[str, list[str]]) -> None:
            """Lê a revisão web da fonte sem fazer consultas externas."""
            key = query.get("site", [""])[0]
            if key not in sites:
                self._json(404, {"error": "fonte não encontrada"})
                return
            identity = hashlib.sha256(key.encode()).hexdigest()
            report = audit.directory / "revisao-web-fontes/detalhes" / f"{identity}.json"
            try:
                data = json.loads(report.read_text()) if report.is_file() else {"available": False}
            except (OSError, ValueError):
                self._json(503, {"error": "revisão web indisponível"})
                return
            self._json(200, data)

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
                    progress_path=Path(__file__).resolve().parents[3]
                    / "var/progresso-descoberta.json",
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

        def _send(
            self,
            status: int,
            content_type: str,
            body: bytes,
            *,
            csp: str = _CSP,
            download: bool = False,
        ) -> None:
            """Envia a resposta com cabeçalhos de segurança e sem cache."""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", csp)
            if download:
                self.send_header("Content-Disposition", "attachment")
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

        def _catalog_write(self) -> None:
            """Escreve apenas no cadastro novo; rotas históricas permanecem somente leitura."""
            if urlsplit(self.path).path.startswith("/api/gerenciador/"):
                self._json(*manager.handle(self))
            else:
                self._not_allowed()

        do_POST = do_DELETE = do_PATCH = _catalog_write  # noqa: N815
        do_PUT = _not_allowed  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Silencia o log por requisição (o painel consulta a cada poucos segundos)."""

    class ManagerServer(ThreadingHTTPServer):
        def server_close(self) -> None:
            """Fecha também o executor limitado dos testes interativos."""
            if manager.health_worker:
                manager.health_worker.close()
            manager.jobs.close()
            super().server_close()

    server = ManagerServer((host, port), Handler)
    if manager.health_worker:
        manager.health_worker.start()
    return server


def _bounded(query: dict[str, list[str]], name: str, default: int, maximum: int) -> int:
    """Parâmetro inteiro de consulta entre 1 e ``maximum``; inválido cai no padrão."""
    try:
        value = int(query[name][0])
    except (KeyError, IndexError, ValueError):
        return default
    return min(max(value, 1), maximum)
