"""Schema descartável por teste para o novo desenho (tabelas ``bm_*``)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql

from buscador_midias.db.connection import connect


@pytest.fixture
def schema_name() -> Iterator[str]:
    """Nome de um schema que só existe durante o teste."""
    name = "bm_test_db_" + uuid.uuid4().hex[:16]
    yield name
    with connect() as db:
        db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def db(schema_name: str) -> Iterator[psycopg.Connection[Any]]:
    """Conexão com ``search_path`` no schema do teste; o DDL é instalado pelo próprio teste."""
    with connect(schema_name, create=True) as connection:
        yield connection
