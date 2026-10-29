"""Conexão com o Postgres do buscador: credenciais do ambiente e schema por ``search_path``."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DEFAULT_SCHEMA = "public"
_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
_KEYS = ("HOST", "PORT", "DATABASE", "USERNAME", "PASSWORD")


class DatabaseConfigError(RuntimeError):
    """Credenciais ou schema inválidos; a mensagem nunca traz valores secretos."""


def validate_schema(schema: str) -> str:
    """Aceita ``public`` ou um schema próprio; recusa nomes do sistema."""
    if not _NAME.fullmatch(schema) or schema == "information_schema" or schema.startswith("pg_"):
        raise DatabaseConfigError("Informe um nome de schema válido.")
    return schema


def configured_schema(env: Mapping[str, str] | None = None) -> str:
    """Schema das tabelas do buscador: ``BM_SCHEMA`` ou ``public``."""
    values = os.environ if env is None else env
    return validate_schema(values.get("BM_SCHEMA") or DEFAULT_SCHEMA)


def parameters(schema: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Parâmetros do conector, com timeouts e sem consultas paralelas."""
    values = os.environ if env is None else env
    if any(not values.get("DB_MIDIAS_IA_" + name) for name in _KEYS):
        raise DatabaseConfigError("Configure DB_MIDIAS_IA_HOST/PORT/DATABASE/USERNAME/PASSWORD.")
    return dict(
        host=values["DB_MIDIAS_IA_HOST"],
        port=values["DB_MIDIAS_IA_PORT"],
        dbname=values["DB_MIDIAS_IA_DATABASE"],
        user=values["DB_MIDIAS_IA_USERNAME"],
        password=values["DB_MIDIAS_IA_PASSWORD"],
        connect_timeout=10,
        # O contêiner tem /dev/shm de 64 MB: consultas paralelas esgotavam esse segmento.
        options=(
            f"-c search_path={schema},pg_catalog -c timezone=UTC -c lock_timeout=15000 "
            "-c statement_timeout=120000 -c max_parallel_workers_per_gather=0"
        ),
    )


def connect(
    schema: str | None = None, *, env: Mapping[str, str] | None = None, create: bool = False
) -> psycopg.Connection[Any]:
    """Abre uma conexão no schema pedido; ``create`` cria o schema antes (testes e instalação)."""
    name = validate_schema(schema) if schema else configured_schema(env)
    if create and name != DEFAULT_SCHEMA:
        with psycopg.connect(**parameters("public", env), autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(name)))
    return psycopg.connect(**parameters(name, env))


class Repository:
    """Base dos repositórios: schema e ambiente, e uma transação por operação.

    Sem pool, cada operação abre a sua conexão (ferramentas, testes). O serviço liga o pool com
    ``start_pool`` para não reconectar a cada checkpoint.
    """

    def __init__(self, schema: str | None = None, env: Mapping[str, str] | None = None) -> None:
        """O schema padrão vem de ``BM_SCHEMA`` (``public`` se ausente)."""
        self.schema = schema or configured_schema(env)
        self.env = env
        self.pool: ConnectionPool[Any] | None = None

    def start_pool(self, *, min_size: int = 2, max_size: int = 12, name: str = "bm") -> None:
        """Liga um pool limitado de conexões curtas; as de lock longo continuam à parte."""
        if self.pool is not None:
            return
        pool: ConnectionPool[Any] = ConnectionPool(
            kwargs={**parameters(self.schema, self.env), "row_factory": dict_row},
            min_size=min_size,
            max_size=max_size,
            max_idle=10,
            timeout=10,
            name=name,
            open=False,
        )
        pool.open(wait=True, timeout=10)
        self.pool = pool

    def close_pool(self) -> None:
        """Fecha o pool depois da drenagem dos consumidores; repetir é seguro."""
        pool, self.pool = self.pool, None
        if pool is not None:
            pool.close()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        """Conexão com linhas em dicionário; falha desfaz tudo, inclusive a auditoria."""
        if self.pool is not None:
            with self.pool.connection() as pooled:
                yield pooled
            return
        with self.direct_connection() as db:
            yield db

    @contextmanager
    def direct_connection(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        """Conexão própria, fora do pool: para locks que atravessam um HTTP."""
        with connect(self.schema, env=self.env) as db:
            db.row_factory = dict_row
            yield db
