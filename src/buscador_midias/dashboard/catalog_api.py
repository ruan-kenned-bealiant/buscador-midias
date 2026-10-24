"""API local de escrita do gerenciador, isolada das consultas históricas."""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg

from buscador_midias.catalog.batch import status as validation_status
from buscador_midias.catalog.bright_reader import bright_data_available
from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.health_worker import HealthWorker
from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.catalog.store import CatalogError, CatalogStore
from buscador_midias.catalog.testing import TestJobs
from buscador_midias.ingestion.store import IngestStore

_LOG = logging.getLogger(__name__)
_BASE = "/api/gerenciador"
_SOURCE = re.compile(r"/fontes/([1-9]\d{0,12})(/mecanismos)?$")
_LINK = re.compile(r"/mecanismos/([1-9]\d{0,12})$")
_MODEL = re.compile(r"/modelos/([a-z_]{1,40})$")


@dataclass
class CatalogAPI:
    """Roteia cadastros com JSON limitado, proteção de origem e controle de concorrência."""

    store: CatalogStore
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    jobs: TestJobs = field(init=False)
    ingestion: IngestStore | None = field(init=False, default=None)
    health: HealthStore | None = field(init=False, default=None)
    health_worker: HealthWorker | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        """Inicia o executor limitado de testes sob demanda."""
        self.jobs = TestJobs(self.store)
        if isinstance(self.store, PostgresCatalogStore):
            self.ingestion = IngestStore(self.store)
            self.health = HealthStore(self.store)
            self.health_worker = HealthWorker(self.health)

    def handle(self, handler: BaseHTTPRequestHandler) -> tuple[int, dict[str, Any]]:
        """Produz uma resposta segura para uma requisição do gerenciador."""
        try:
            return self._dispatch(handler)
        except CatalogError as error:
            return error.status, dict(error=str(error))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return 400, dict(error="Requisição inválida.")
        except (sqlite3.Error, psycopg.Error, OSError):
            _LOG.exception("Falha no cadastro local")
            return 503, dict(error="Cadastro indisponível. Tente novamente.")

    def _dispatch(self, handler: BaseHTTPRequestHandler) -> tuple[int, dict[str, Any]]:
        parts = urlsplit(handler.path)
        path = parts.path.removeprefix(_BASE)
        method = handler.command
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        source, link = _SOURCE.fullmatch(path), _LINK.fullmatch(path)
        model = _MODEL.fullmatch(path)
        if path.startswith("/busca"):
            from buscador_midias.ingestion.api import dispatch

            return dispatch(self, handler, path, query)
        if path.startswith("/saude"):
            if self.health is None:
                raise CatalogError("Saúde das fontes requer o catálogo PostgreSQL.", 503)
            offset = int(query.get("offset", "0"))
            if not 0 <= offset <= 1000000:
                raise CatalogError("Paginação inválida.")
            run_match = re.fullmatch(r"/saude/rodadas/([a-f0-9]{32})(/cancelar)?", path)
            setting_match = re.fullmatch(r"/saude/configuracoes/(active|recovery)", path)
            if method == "GET":
                if path == "/saude":
                    health_result = self.health.overview(offset)
                elif run_match and not run_match[2]:
                    health_result = self.health.details(
                        run_match[1], offset, query.get("category", "")
                    )
                else:
                    raise CatalogError("Endereço não encontrado.", 404)
                return 200, self.health.jsonable(health_result)
            self._authorize(handler)
            data = self._body(handler)
            if method == "PATCH" and setting_match:
                return 200, self.health.jsonable(self.health.configure(setting_match[1], data))
            if method == "POST" and path == "/saude/rodadas" and "kind" in data:
                created = self.health.create(
                    data["kind"], options={k: v for k, v in data.items() if k != "kind"}
                )
                if self.health_worker:
                    self.health_worker.wake.set()
                return 202, self.health.jsonable(created)
            if method == "POST" and run_match and run_match[2] and not data:
                return 200, self.health.jsonable(self.health.cancel(run_match[1]))
            raise CatalogError("Endereço ou método não permitido.", 404)
        if method == "GET":
            if path == "/resumo":
                return 200, dict(
                    **self.store.summary(),
                    csrf_token=self.token,
                    bright_data_available=bright_data_available(),
                )
            if path == "/validacao":
                return 200, validation_status(self.store)
            if path == "/modelos":
                return 200, dict(items=self.store.models())
            if model:
                return 200, self.store.model_settings(model[1])
            if re.fullmatch(r"/testes/[a-f0-9]{32}", path):
                return 200, self.jobs.get(path.rsplit("/", 1)[1])
            offset = int(query.get("offset", "0"))
            limit = int(query.get("limit", "25"))
            if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
                raise CatalogError("Paginação inválida.", 400)
            status, kind = query.get("status", "all"), query.get("kind", "all")
            if status not in ("all", "active", "inactive") or kind not in (
                "all",
                "feed",
                "sitemap",
            ):
                raise CatalogError("Filtro inválido.", 400)
            if path == "/fontes":
                return 200, self.store.sources(
                    query.get("q", "")[:200],
                    status,
                    query.get("country", "")[:100],
                    offset,
                    limit,
                )
            if source:
                ident = int(source[1])
                if source[2]:
                    return 200, self.store.endpoints(
                        ident,
                        query.get("q", "")[:200],
                        status,
                        kind,
                        offset,
                        limit,
                    )
                return 200, self.store.source(ident)
            raise CatalogError("Endereço não encontrado.", 404)
        if method not in ("POST", "PATCH", "DELETE"):
            raise CatalogError("Método não permitido.", 405)
        self._authorize(handler)
        data = self._body(handler)
        if method == "PATCH" and model:
            return 200, self.store.save_model(model[1], data)
        if method == "POST":
            if path == "/testar":
                return 202, self.jobs.start(data)
            if path == "/fontes":
                return 201, self.store.save("source", data)
            if source and source[2]:
                result = self.jobs.result(data.pop("test_id")) if "test_id" in data else None
                return 201, self.store.save(
                    "endpoint", data, source_id=int(source[1]), test_result=result
                )
        elif (source and not source[2]) or link:
            entity = "source" if source else "endpoint"
            ident = int(source[1] if source else link[1])  # type: ignore[index]
            if method == "PATCH":
                result = (
                    self.jobs.result(data.pop("test_id"))
                    if entity == "endpoint" and "test_id" in data
                    else None
                )
                return 200, self.store.save(entity, data, ident, test_result=result)
            self.store.delete(entity, ident, data.get("version"))
            return 200, dict(deleted=True)
        raise CatalogError("Endereço ou método não permitido.", 404)

    def _authorize(self, handler: BaseHTTPRequestHandler) -> None:
        """Restringe escrita ao navegador local com token e origem do próprio servidor."""
        host = handler.headers.get("Host", "")
        try:
            parsed = urlsplit(f"http://{host}")
            port = parsed.port or 80
        except ValueError as error:
            raise CatalogError("Origem inválida.", 403) from error
        if (
            parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or not isinstance(handler.server, TCPServer)
            or port != handler.server.server_address[1]
        ):
            raise CatalogError("Edições são permitidas somente no painel local.", 403)
        origin = handler.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            raise CatalogError("Origem não permitida.", 403)
        if not secrets.compare_digest(handler.headers.get("X-Catalog-Token", ""), self.token):
            raise CatalogError("Atualize a página antes de editar o cadastro.", 403)

    @staticmethod
    def _body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        if handler.headers.get_content_type() != "application/json":
            raise CatalogError("Envie os dados como JSON.", 415)
        if handler.headers.get("Transfer-Encoding"):
            raise CatalogError("Formato de envio não permitido.", 400)
        size = int(handler.headers.get("Content-Length", "0"))
        if not 0 < size <= 32_768:
            raise CatalogError("Tamanho de envio inválido.", 413)
        handler.connection.settimeout(10)
        data = json.loads(handler.rfile.read(size))
        if not isinstance(data, dict):
            raise CatalogError("Envie um objeto JSON.", 400)
        return data
