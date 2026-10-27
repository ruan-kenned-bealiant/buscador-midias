"""Pool limitado para transações curtas; locks de rede mantêm conexões próprias."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from buscador_midias.catalog.postgres import postgres_parameters


class IngestConnections:
    """Evita reconectar por checkpoint sem emprestar conexões entre threads simultâneas."""

    def __init__(
        self,
        schema: str,
        env: Mapping[str, str] | None = None,
        *,
        name: str = "ingest-short",
        min_size: int = 2,
    ) -> None:
        """Abre no máximo doze conexões curtas por processo de ingestão."""
        self.schema = schema
        self.pool: ConnectionPool[Any] = ConnectionPool(
            kwargs={**postgres_parameters(env), "row_factory": dict_row},
            min_size=min_size,
            max_size=12,
            max_idle=30,
            timeout=10,
            name=name,
            open=False,
        )
        self.pool.open(wait=True, timeout=10)

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Commit/rollback na devolução e search_path restrito à transação emprestada."""
        with self.pool.connection() as db:
            db.execute(
                sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(
                    sql.Identifier(self.schema)
                )
            )
            yield db

    def close(self) -> None:
        """Libera recursos depois do encerramento cooperativo dos workers."""
        self.pool.close()
