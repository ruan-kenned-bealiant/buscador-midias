# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Inicializar o esquema não pode competir por locks com o serviço em execução."""

from __future__ import annotations

import os
import time

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest  # noqa: F401

from buscador_midias.ingestion import store as store_module
from buscador_midias.ingestion.schema import SCHEMA_VERSION
from buscador_midias.ingestion.store import IngestStore

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def recorded(ingest):
    with ingest.connection() as db:
        return db.execute("SELECT max(version) v FROM ingest_schema").fetchone()["v"]


def test_initialize_records_the_schema_version(ingest):
    assert recorded(ingest) == SCHEMA_VERSION


def test_initialize_skips_the_ddl_when_the_version_is_current(ingest, monkeypatch):
    monkeypatch.setattr(store_module, "DDL", "SELECT 1/0")
    IngestStore(ingest.store).initialize()


def test_initialize_applies_the_ddl_and_records_when_the_version_is_older(ingest, monkeypatch):
    with ingest.connection() as db:
        db.execute("DELETE FROM ingest_schema")
        db.execute("DROP TRIGGER ingest_outbox_host ON ingest_outbox")
    IngestStore(ingest.store).initialize()
    assert recorded(ingest) == SCHEMA_VERSION
    with ingest.connection() as db:
        trigger = db.execute(
            "SELECT count(*) n FROM pg_trigger WHERE tgname='ingest_outbox_host'"
        ).fetchone()["n"]
    assert trigger == 1


def test_initialize_does_not_wait_for_locks_held_on_the_queue_tables(ingest):
    with ingest.connection() as blocker:
        blocker.execute("LOCK TABLE ingest_outbox IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        IngestStore(ingest.store).initialize()
        assert time.monotonic() - started < 5
