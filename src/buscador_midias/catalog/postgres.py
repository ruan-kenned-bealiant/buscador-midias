"""Repositório PostgreSQL: mantém o contrato do gerenciador e usa transações reais."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from buscador_midias.catalog.store import CatalogError, CatalogStore

DEFAULT_SCHEMA = "midias_manager"
WRITE_LOCK = 674931205


def connect_postgres(env: Mapping[str, str] | None = None) -> psycopg.Connection[Any]:
    """Usa exclusivamente as credenciais de Mídias, sem incluí-las em mensagens."""
    return psycopg.connect(**postgres_parameters(env))


def postgres_parameters(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Parâmetros comuns ao conector e ao pool, nunca expostos pela API."""
    values = os.environ if env is None else env
    names = ("HOST", "PORT", "DATABASE", "USERNAME", "PASSWORD")
    if any(not values.get("DB_MIDIAS_IA_" + name) for name in names):
        raise CatalogError("Configure DB_MIDIAS_IA_HOST/PORT/DATABASE/USERNAME/PASSWORD.", 503)
    return dict(
        host=values["DB_MIDIAS_IA_HOST"],
        port=values["DB_MIDIAS_IA_PORT"],
        dbname=values["DB_MIDIAS_IA_DATABASE"],
        user=values["DB_MIDIAS_IA_USERNAME"],
        password=values["DB_MIDIAS_IA_PASSWORD"],
        connect_timeout=10,
        # Sem consultas paralelas: o contêiner tem /dev/shm de 64 MB e o paralelismo esgotava
        # esse segmento ("DiskFull") em consultas que já são curtas.
        options=(
            "-c timezone=UTC -c lock_timeout=15000 -c statement_timeout=120000 "
            "-c max_parallel_workers_per_gather=0"
        ),
    )


def validate_schema(schema: str) -> str:
    """Restringe o destino a um namespace próprio, nunca public ou esquemas do sistema."""
    if (
        not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema)
        or schema
        in {
            "public",
            "information_schema",
        }
        or schema.startswith("pg_")
    ):
        raise ValueError("Informe um esquema próprio válido para o gerenciador.")
    return schema


class CatalogRow:
    """Compatibilidade do contrato interno: índices, nomes e dict(row), como sqlite.Row."""

    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        """Mantém valores imutáveis e ordem das colunas."""
        self.names = names
        self.values = tuple(
            json.dumps(v, ensure_ascii=False)
            if isinstance(v, (dict, list))
            else v.isoformat()
            if isinstance(v, datetime)
            else v
            for v in values
        )

    def keys(self) -> Sequence[str]:
        """Nomes das colunas para construir os DTOs existentes."""
        return self.names

    def __getitem__(self, key: Any) -> Any:
        """Aceita nome, posição ou fatia."""
        return self.values[self.names.index(key) if isinstance(key, str) else key]

    def __iter__(self) -> Iterator[Any]:
        """Itera valores para pares e agregações."""
        return iter(self.values)


def row_factory(cursor: Any) -> Any:
    """Traduz tipos nativos somente na borda do contrato atual da API."""
    names = tuple(c.name for c in cursor.description or ())
    return lambda values: CatalogRow(names, values)


class CatalogConnection:
    """Adapta o pequeno dialeto de consultas compartilhadas, sem reimplementar regras do CRUD."""

    def __init__(self, db: psycopg.Connection[Any], schema: str) -> None:
        """Associa a transação já aberta e o namespace do catálogo."""
        self.db, self.schema = db, schema

    def execute(self, query: str, parameters: Sequence[Any] = ()) -> Any:
        """Executa parâmetros vinculados e adapta explicitamente as expressões SQLite usadas."""
        if query == "BEGIN IMMEDIATE":
            return self.db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
        if query == "SELECT 1 FROM sqlite_master WHERE name='catalog_validation_runs'":
            return self.db.execute(
                "SELECT 1 FROM information_schema.tables WHERE "
                "table_schema=%s AND table_name='catalog_validation_runs'",
                (self.schema,),
            )
        query = query.replace("?", "%s")
        query = query.replace(
            "SUM(e.active AND s.active)",
            "SUM(CASE WHEN e.active=1 AND s.active=1 THEN 1 ELSE 0 END)",
        )
        query = query.replace("SUM(kind='feed')", "SUM((kind='feed')::int)")
        query = query.replace("SUM(kind='sitemap')", "SUM((kind='sitemap')::int)")
        query = query.replace("s.name COLLATE NOCASE", "lower(s.name)")
        query = re.sub(
            r"instr\(catalog_fold\(([^)]+)\),catalog_fold\(([^)]+)\)\)",
            r"(strpos(catalog_fold(\1),catalog_fold(\2))>0)",
            query,
        )
        if query.startswith("INSERT OR IGNORE INTO "):
            query = query.replace("INSERT OR IGNORE INTO ", "INSERT INTO ", 1)
            query += " ON CONFLICT DO NOTHING"
        identity = re.match(r"INSERT INTO (sites|links)\(", query)
        try:
            return self._execute(query, parameters, bool(identity))
        except psycopg.IntegrityError as error:
            raise sqlite3.IntegrityError(
                "Conflito de integridade no catálogo PostgreSQL."
            ) from error

    def _execute(self, query: str, parameters: Sequence[Any], identity: bool) -> Any:
        if identity:
            cursor = self.db.execute(query + " RETURNING id", parameters)
            inserted = cursor.fetchone()
            assert inserted is not None
            ident = inserted[0]
            return type("Inserted", (), {"lastrowid": ident})()
        return self.db.execute(query, parameters)


class PostgresCatalogStore(CatalogStore):
    """Mesmo domínio do catálogo, com PostgreSQL como única base editável."""

    backend = "postgres"

    def __init__(self, schema: str = DEFAULT_SCHEMA, env: Mapping[str, str] | None = None) -> None:
        """Nunca importa JSON/CSV nem SQLite automaticamente ao abrir o repositório."""
        self.schema, self.env = validate_schema(schema), env
        # Compatibilidade com consumidores que exibem a origem do repositório.
        super().__init__(Path("postgresql") / self.schema, Path(), Path())

    def initialize(self) -> None:
        """Recusa esquema não migrado; não cria tabelas durante uma requisição."""
        if self._ready:
            return
        with connect_postgres(self.env) as db:
            found = db.execute(
                "SELECT to_regclass(%s)", (self.schema + ".catalog_imports",)
            ).fetchone()
            if found is None or not found[0]:
                raise CatalogError("Migre o catálogo para PostgreSQL antes de abrir o painel.", 503)
            if not db.execute(
                sql.SQL("SELECT 1 FROM {}.catalog_imports LIMIT 1").format(
                    sql.Identifier(self.schema)
                )
            ).fetchone():
                raise CatalogError("Importação do catálogo ainda não concluída.", 503)
        self._ready = True

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Uma conexão/transação por operação; falhas desfazem inclusive a auditoria."""
        self.initialize()
        with connect_postgres(self.env) as db:
            db.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(self.schema)
                )
            )
            db.row_factory = row_factory
            yield CatalogConnection(db, self.schema)
