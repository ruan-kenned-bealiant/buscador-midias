"""Servidor local do painel: páginas, API do cadastro, da saúde e da busca, e a evidência."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.catalog.testing import ReaderFactory
from buscador_midias.dashboard.catalog_api import CatalogAPI
from buscador_midias.dashboard.guide import render_guide
from buscador_midias.db.catalog import CatalogRepository
from buscador_midias.db.serialize import jsonable
from buscador_midias.ingestion.evidence_view import read_captured

_CSP = (
    "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
)
_VIEWER_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'unsafe-inline' http: https:; "
    "connect-src 'self'; frame-src 'self'; img-src http: https: data:; "
    "font-src http: https: data:; base-uri http: https:; form-action 'none'"
)
_RAW_CSP = "sandbox; script-src 'none'; object-src 'none'; form-action 'none'"
_EVIDENCE = re.compile(r"/evidencias/corpus/([a-f0-9]{64})")
_LOG = logging.getLogger(__name__)

#: Páginas HTML servidas na raiz e o arquivo de cada uma.
_PAGES = {
    "/": "panel.html",
    "/gerenciador": "manager.html",
    "/mecanismos": "manager.html",
    "/saude": "manager.html",
    "/buscador": "search.html",
}
_SCRIPTS = {
    "/assets/app.js": "app.js",
    "/assets/panel.js": "panel.js",
    "/assets/health.js": "health.js",
    "/assets/search.js": "search.js",
    "/assets/evidence.js": "evidence.js",
    "/assets/evidence_prepare.js": "evidence_prepare.js",
}


def _asset(name: str) -> bytes:
    return resources.files("buscador_midias.dashboard").joinpath(name).read_bytes()


def make_server(
    host: str,
    port: int,
    *,
    schema: str | None = None,
    corpus_schema: str = "public",
    broker: Callable[[], list[dict[str, Any]]] | None = None,
    test_reader_factory: ReaderFactory | None = None,
) -> ThreadingHTTPServer:
    """Cria o servidor do painel (sem iniciá-lo).

    Args:
        host: Endereço de escuta (use ``127.0.0.1``: o painel só aceita escrita local).
        port: Porta (``0`` escolhe uma livre).
        schema: Schema das tabelas ``bm_*`` (padrão: ``BM_SCHEMA`` ou ``public``).
        corpus_schema: Schema do corpus (``midias_ia_articles``).
        broker: Leitor da profundidade das filas, substituível em testes sem RabbitMQ.
        test_reader_factory: Leitor dos testes interativos, substituível em testes sem rede.

    Returns:
        O servidor, pronto para ``serve_forever``.
    """
    pages = {path: _asset(name) for path, name in _PAGES.items()}
    scripts = {
        path: ("application/javascript; charset=utf-8", _asset(name))
        for path, name in _SCRIPTS.items()
    }
    scripts["/assets/app.css"] = ("text/css; charset=utf-8", _asset("app.css"))
    guide = render_guide()
    viewer = _asset("evidence.html")
    manager = CatalogAPI(CatalogRepository(schema), broker=broker, corpus_schema=corpus_schema)
    if test_reader_factory is not None:
        manager.jobs.factory = test_reader_factory

    class Handler(BaseHTTPRequestHandler):
        """Atende as páginas, a API e a evidência; só o cadastro novo aceita escrita."""

        server_version = "BuscadorPainel"

        def do_GET(self) -> None:  # noqa: N802 - nome exigido pelo http.server
            """Roteia as requisições GET."""
            parts = urlsplit(self.path)
            if parts.path in pages:
                self._send(200, "text/html; charset=utf-8", pages[parts.path])
            elif parts.path == "/guia":
                self._send(200, "text/html; charset=utf-8", guide)
            elif parts.path in scripts:
                self._send(200, *scripts[parts.path])
            elif parts.path.startswith("/evidencias/"):
                self._evidence(parts)
            elif parts.path.startswith("/api/gerenciador/"):
                self._json(*manager.handle(self))
            else:
                self._json(404, {"error": "Endereço não encontrado."})

        def _evidence(self, parts: Any) -> None:
            """A evidência registrada no corpus, vista no visualizador ou como arquivo original."""
            match = _EVIDENCE.fullmatch(parts.path)
            if not match or manager.reports is None:
                self._json(404, {"error": "Evidência não encontrada."})
                return
            try:
                stored = manager.reports.evidence(match[1]) or {}
                content_type, data = read_captured(stored.get("path"), stored.get("status"))
                original = parse_qs(parts.query).get("original") == ["1"]
                if content_type == "text/html" and not original:
                    self._send(200, "text/html; charset=utf-8", viewer, csp=_VIEWER_CSP)
                    return
                self._send(200, content_type, data, csp=_RAW_CSP, download=original)
            except CatalogError as error:
                self._json(error.status, {"error": str(error)})
            except (OSError, psycopg.Error, ValueError):
                self._json(503, {"error": "Evidência indisponível. Tente novamente."})

        def _json(self, status: int, payload: dict[str, object]) -> None:
            """Responde com JSON."""
            body = json.dumps(jsonable(payload), ensure_ascii=False).encode("utf-8")
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

        def _write(self) -> None:
            """Escreve apenas no cadastro novo, sob token e origem local."""
            if urlsplit(self.path).path.startswith("/api/gerenciador/"):
                self._json(*manager.handle(self))
            else:
                self._send_not_allowed()

        def _send_not_allowed(self) -> None:
            """Recusa métodos que não sejam GET fora da API."""
            body = json.dumps({"error": "método não permitido"}).encode("utf-8")
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_DELETE = do_PATCH = _write  # noqa: N815
        do_PUT = _send_not_allowed  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Silencia o log por requisição (o painel consulta a cada poucos segundos)."""

    class ManagerServer(ThreadingHTTPServer):
        def server_close(self) -> None:
            """Fecha também o executor limitado dos testes interativos."""
            manager.jobs.close()
            super().server_close()

    return ManagerServer((host, port), Handler)
