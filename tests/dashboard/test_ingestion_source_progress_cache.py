# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Aba "Por site": virar página ou buscar não recalcula todas as fontes da rodada."""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest  # noqa: F401
from test_ingestion_fast_batch import prepare

from buscador_midias.ingestion import progress
from buscador_midias.ingestion.progress import source_progress

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.fixture
def counted(ingest, monkeypatch):
    """Conta as conexões abertas ao banco e zera o cache do módulo entre os testes."""
    progress._ROWS.clear()
    calls = []
    original = ingest.connection

    @contextmanager
    def connection():
        calls.append(1)
        with original() as db:
            yield db

    monkeypatch.setattr(ingest, "connection", connection)
    return calls


def test_without_a_cache_every_call_reads_the_database(ingest, counted):
    run, _ = prepare(ingest, 2)
    before = len(counted)
    source_progress(ingest, run["id"])
    one_call = len(counted) - before
    source_progress(ingest, run["id"])
    assert one_call >= 1 and len(counted) - before == 2 * one_call


def test_with_a_cache_paging_and_searching_reuse_the_computed_rows(ingest, counted):
    run, _ = prepare(ingest, 2)
    first = source_progress(ingest, run["id"], cache_seconds=60)
    reads = len(counted)
    again = source_progress(ingest, run["id"], offset=25, cache_seconds=60)
    named = source_progress(ingest, run["id"], search="jos", cache_seconds=60)
    unknown = source_progress(ingest, run["id"], search="inexistente", cache_seconds=60)

    assert len(counted) == reads  # nenhuma consulta nova
    assert first["total"] == 1 and again["sources"] == [] and again["total"] == 1
    assert [s["name"] for s in named["sources"]] == [s["name"] for s in first["sources"]]
    assert unknown["total"] == 0
    assert first["summary"] == again["summary"] == named["summary"]


def test_the_cache_expires(ingest, counted, monkeypatch):
    run, _ = prepare(ingest, 2)
    clock = [1000.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])
    before = len(counted)
    source_progress(ingest, run["id"], cache_seconds=15)
    one_call = len(counted) - before
    reads = len(counted)
    clock[0] += 14
    source_progress(ingest, run["id"], cache_seconds=15)
    assert len(counted) == reads
    clock[0] += 2
    source_progress(ingest, run["id"], cache_seconds=15)
    assert len(counted) == reads + one_call


def test_the_cache_is_kept_per_run(ingest, counted):
    first, _ = prepare(ingest, 2)
    source_progress(ingest, first["id"], cache_seconds=60)
    reads = len(counted)
    with ingest.connection() as db:
        db.execute("UPDATE ingest_runs SET finished_at=now(),state='completed'")
        db.execute("UPDATE ingest_polls SET discovery_done=true")
    other = ingest.create_run({})
    source_progress(ingest, other["id"], cache_seconds=60)
    assert len(counted) > reads


def test_cached_and_fresh_results_are_identical(ingest):
    progress._ROWS.clear()
    run, _ = prepare(ingest, 2)
    fresh = source_progress(ingest, run["id"])
    cached = source_progress(ingest, run["id"], cache_seconds=60)
    assert fresh == cached
