"""Servidor HTTP local do painel: uma página e um endpoint JSON, só leitura."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from buscador_midias.dashboard.snapshot import DEFAULT_HOURS, DEFAULT_RECENT, build_snapshot
from buscador_midias.state.store import StateStore

DEFAULT_CACHE_SECONDS = 4.0
_MAX_HOURS = 168
_MAX_RECENT = 200
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
)
_LOG = logging.getLogger(__name__)


def make_server(
    db: Path,
    host: str,
    port: int,
    *,
    clock: Callable[[], datetime] | None = None,
    schedule_path: Path | None = None,
    cache_seconds: float = DEFAULT_CACHE_SECONDS,
) -> ThreadingHTTPServer:
    """Cria o servidor do painel (sem iniciá-lo).

    O estado é aberto somente leitura a cada requisição e fechado em seguida, então o painel
    nunca grava nem segura o banco enquanto a descoberta e a triagem trabalham.

    Args:
        db: Arquivo SQLite do estado.
        host: Endereço de escuta (use ``127.0.0.1``: o painel não tem autenticação).
        port: Porta (``0`` escolhe uma livre).
        clock: Relógio (injetável); padrão é o horário atual em UTC.
        schedule_path: JSON com as execuções previstas, relido a cada requisição (opcional).
        cache_seconds: Por quanto tempo o retrato calculado é reaproveitado entre as requisições
            (várias abas ou atualizações seguidas não recalculam tudo).

    Returns:
        O servidor, pronto para ``serve_forever``.
    """
    now_fn = clock or (lambda: datetime.now(UTC))
    cache: dict[tuple[int, int], tuple[datetime, bytes]] = {}
    cache_lock = threading.Lock()
    page = resources.files("buscador_midias.dashboard").joinpath("index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        """Atende ``GET /`` (página) e ``GET /api/snapshot`` (dados)."""

        server_version = "BuscadorPainel"

        def do_GET(self) -> None:  # noqa: N802 - nome exigido pelo http.server
            """Roteia as requisições GET."""
            parts = urlsplit(self.path)
            if parts.path == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif parts.path == "/api/snapshot":
                self._snapshot(parse_qs(parts.query))
            else:
                self._json(404, {"error": "não encontrado"})

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
                store = StateStore(db, read_only=True)
            except (sqlite3.Error, OSError):
                return None
            try:
                snapshot = build_snapshot(
                    store,
                    now=now,
                    hours=hours,
                    recent=recent,
                    schedule_path=schedule_path,
                )
            except sqlite3.Error:
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
