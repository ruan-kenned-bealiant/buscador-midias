# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Custo do ciclo do publicador: faxina periódica e leitura dos hosts por índice."""

from __future__ import annotations

import os

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest, next_job  # noqa: F401

from buscador_midias.ingestion.admission import primary_hosts
from buscador_midias.ingestion.runtime import Service

needs_postgres = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def seed(ingest):
    run = ingest.create_run({})
    root = next_job(ingest, "discovery")
    ingest.finish(root, {})
    return run, root


def add_job(ingest, run, root, stage, url, **changes):
    """Cria uma tarefa com evento na outbox e aplica as alterações de estado pedidas."""
    with ingest.connection() as db:
        ident = ingest.enqueue(db, stage, url, run["id"], root["poll_id"], {"url": url})
        for column, value in changes.items():
            db.execute(f"UPDATE ingest_jobs SET {column}=%s WHERE id=%s", (value, ident))
    return ident


def discarded(ingest, ident):
    with ingest.connection() as db:
        row = db.execute(
            "SELECT discarded_at FROM ingest_outbox WHERE job_id=%s", (ident,)
        ).fetchone()
    return row["discarded_at"] is not None


@needs_postgres
def test_obsolete_events_are_discarded_on_the_first_publication_cycle(ingest):
    run, root = seed(ingest)
    done = add_job(ingest, run, root, "dedup", "https://a.example/1", state="succeeded")
    assert Service(ingest).publish_once(None, "dedup") == 0
    assert discarded(ingest, done)


@needs_postgres
def test_the_cleanup_runs_at_most_once_per_interval_for_each_publisher(ingest):
    run, root = seed(ingest)
    first = add_job(ingest, run, root, "dedup", "https://a.example/1", state="succeeded")
    service = Service(ingest)
    service.publish_once(None, "dedup")
    second = add_job(ingest, run, root, "dedup", "https://b.example/1", state="succeeded")
    service.publish_once(None, "dedup")
    assert discarded(ingest, first) and not discarded(ingest, second)
    service.cleanup_every = 0
    service.publish_once(None, "dedup")
    assert discarded(ingest, second)


@needs_postgres
def test_each_stage_and_lane_has_its_own_cleanup_clock(ingest):
    run, root = seed(ingest)
    service = Service(ingest)
    service.publish_once(None, "dedup")
    persisted = add_job(ingest, run, root, "persist", "https://a.example/1", state="succeeded")
    recovery = add_job(ingest, run, root, "dedup", "https://b.example/1", state="succeeded")
    service.publish_once(None, "persist")
    assert discarded(ingest, persisted)
    service.publish_once(None, "dedup", recovery=True)
    assert discarded(ingest, recovery)


@needs_postgres
def test_work_that_is_already_finished_is_never_published_between_cleanups(ingest, monkeypatch):
    run, root = seed(ingest)
    add_job(ingest, run, root, "fetch", "https://a.example/1", state="succeeded")
    service = Service(ingest)
    service.publish_once(None, "fetch")
    stale = add_job(ingest, run, root, "fetch", "https://b.example/1", state="succeeded")
    monkeypatch.setattr(
        "buscador_midias.ingestion.broker.publish",
        lambda *a, **kw: pytest.fail("Tarefa obsoleta não deve ser publicada"),
    )
    assert service.publish_once(None, "fetch") == 0
    assert not discarded(ingest, stale)


@needs_postgres
def test_primary_hosts_lists_only_hosts_with_ready_new_work(ingest):
    run, root = seed(ingest)
    add_job(ingest, run, root, "fetch", "https://novo.example/1", state="pending")
    add_job(ingest, run, root, "discovery", "https://descoberta.example/1", state="pending")
    add_job(ingest, run, root, "fetch", "https://repete.example/1", state="retry", failures=1)
    add_job(ingest, run, root, "fetch", "https://andando.example/1", state="running")
    futuro = add_job(ingest, run, root, "fetch", "https://futuro.example/1", state="pending")
    marcada = add_job(ingest, run, root, "fetch", "https://marcada.example/1", state="pending")
    add_job(ingest, run, root, "dedup", "https://dedup.example/1", state="pending")
    with ingest.connection() as db:
        db.execute(
            "UPDATE ingest_outbox SET ready_at=now()+interval '1 hour' WHERE job_id=%s", (futuro,)
        )
        db.execute(
            "UPDATE ingest_jobs SET payload=payload||'{\"recovery\":true}'::jsonb WHERE id=%s",
            (marcada,),
        )
        assert primary_hosts(db) == {"novo.example", "descoberta.example"}


@needs_postgres
def test_primary_hosts_normalises_case_port_and_trailing_dot(ingest):
    run, root = seed(ingest)
    add_job(ingest, run, root, "fetch", "https://WWW.Exemplo.example.:8080/a", state="pending")
    with ingest.connection() as db:
        assert primary_hosts(db) == {"www.exemplo.example"}


@needs_postgres
def test_primary_hosts_ignores_published_and_discarded_events(ingest):
    run, root = seed(ingest)
    publicado = add_job(ingest, run, root, "fetch", "https://publicado.example/1", state="pending")
    descartado = add_job(
        ingest, run, root, "fetch", "https://descartado.example/1", state="pending"
    )
    with ingest.connection() as db:
        db.execute("UPDATE ingest_outbox SET published_at=now() WHERE job_id=%s", (publicado,))
        db.execute("UPDATE ingest_outbox SET discarded_at=now() WHERE job_id=%s", (descartado,))
        assert primary_hosts(db) == set()
