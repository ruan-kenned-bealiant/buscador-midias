# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Seleção de candidatos de rede por host: custo independente do tamanho da fila."""

from __future__ import annotations

import os

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest, next_job  # noqa: F401
from test_ingestion import seed_unbalanced_discovery as seed_unbalanced
from test_ingestion_fast_batch import prepare

from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion import broker
from buscador_midias.ingestion.engine import Engine, HostBusyError
from buscador_midias.ingestion.runtime import Service
from buscador_midias.ingestion.store import IngestStore

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def configure(repo, **config):
    repo.configure(dict(version=repo.settings()["version"], config=config))


def publish(ingest, monkeypatch, stage="discovery"):
    """Executa um ciclo do publicador sem RabbitMQ e devolve os hosts escolhidos."""
    sent: list[str] = []
    monkeypatch.setattr(broker, "publish", lambda ch, st, ident, **kw: sent.append(ident))
    Service(ingest).publish_once(None, stage)
    with ingest.connection() as db:
        rows = db.execute(
            "SELECT payload->>'url' url FROM ingest_jobs WHERE id=ANY(%s) ORDER BY payload->>'url'",
            (sent,),
        ).fetchall()
    return [r["url"].split("/")[2] for r in rows]


def test_outbox_host_is_filled_for_inserts_made_outside_the_application_code(ingest):
    seed_unbalanced(ingest)
    with ingest.connection() as db:
        rows = db.execute(
            "SELECT DISTINCT host FROM ingest_outbox WHERE stage='discovery' AND host IS NOT NULL"
        ).fetchall()
        missing = db.execute("SELECT count(*) n FROM ingest_outbox WHERE host IS NULL").fetchone()
    assert {r["host"] for r in rows} >= {"jose.example", "other.example"}
    assert missing["n"] == 0


def test_outbox_host_is_filled_on_the_regular_and_the_batch_paths(ingest):
    _, jobs = prepare(ingest, 2)
    with ingest.connection() as db:
        regular = db.execute("SELECT host FROM ingest_outbox WHERE stage='dedup'").fetchall()
    assert {r["host"] for r in regular} == {"jose.example"}
    ingest.finish_many(jobs, [dict(next="fetch", outcome="nova_url")] * 2)
    with ingest.connection() as db:
        batch = db.execute("SELECT host FROM ingest_outbox WHERE stage='fetch'").fetchall()
    assert len(batch) == 2
    assert {r["host"] for r in batch} == {"jose.example"}


def test_outbox_host_ignores_case_port_and_trailing_dot(ingest):
    run = ingest.create_run({})
    root = next_job(ingest, "discovery")
    ingest.finish(root, {})
    with ingest.connection() as db:
        db.execute(
            "INSERT INTO ingest_jobs(id,stage,subject,run_id,poll_id,payload) VALUES"
            "('h1','discovery','h1',%s,%s,'{\"url\":\"https://WWW.Example.COM.:8443/a\"}')",
            (run["id"], root["poll_id"]),
        )
        db.execute("INSERT INTO ingest_outbox(job_id,dispatch,stage) VALUES('h1',1,'discovery')")
        host = db.execute("SELECT host FROM ingest_outbox WHERE job_id='h1'").fetchone()["host"]
    assert host == "www.example.com"


def test_initialize_backfills_unpublished_rows_that_predate_the_column(ingest):
    seed_unbalanced(ingest)
    with ingest.connection() as db:
        db.execute("UPDATE ingest_outbox SET host=NULL")
        # O DDL (e o backfill) só roda quando a versão gravada é anterior à do código.
        db.execute("DELETE FROM ingest_schema")
    IngestStore(ingest.store).initialize()
    with ingest.connection() as db:
        missing = db.execute(
            "SELECT count(*) n FROM ingest_outbox WHERE host IS NULL AND published_at IS NULL"
        ).fetchone()
    assert missing["n"] == 0


def test_candidates_keep_one_task_per_host_by_default(ingest, monkeypatch):
    seed_unbalanced(ingest)
    assert publish(ingest, monkeypatch) == ["jose.example", "other.example"]


def test_candidates_never_pick_a_task_that_is_not_ready_yet(ingest, monkeypatch):
    seed_unbalanced(ingest)
    with ingest.connection() as db:
        db.execute(
            "UPDATE ingest_outbox SET ready_at=now()+interval '1 hour' WHERE job_id IN "
            "(SELECT id FROM ingest_jobs WHERE payload->>'url' LIKE '%%other.example%%')"
        )
    assert publish(ingest, monkeypatch) == ["jose.example"]


def test_host_concurrency_allows_that_many_tasks_per_host(ingest, monkeypatch):
    seed_unbalanced(ingest)
    configure(ingest, host_concurrency=3)
    assert publish(ingest, monkeypatch) == ["jose.example"] * 3 + ["other.example"]


def test_host_concurrency_counts_tasks_already_in_flight(ingest, monkeypatch):
    seed_unbalanced(ingest)
    configure(ingest, host_concurrency=2)
    with ingest.connection() as db:
        db.execute("UPDATE ingest_jobs SET state='queued' WHERE subject='fair-1'")
    assert publish(ingest, monkeypatch) == ["jose.example", "other.example"]


@pytest.mark.parametrize("value", [0, 5, "2", True, None, 1.5])
def test_host_concurrency_is_validated(ingest, value):
    with pytest.raises(CatalogError):
        configure(ingest, host_concurrency=value)


def test_host_concurrency_defaults_to_one_and_accepts_the_valid_range(ingest):
    assert ingest.settings()["config"]["host_concurrency"] == 1
    for value in (1, 2, 4):
        configure(ingest, host_concurrency=value)
        assert ingest.settings()["config"]["host_concurrency"] == value


def test_execution_lock_admits_only_the_configured_number_of_readers_per_host(ingest):
    engine = Engine(ingest)
    with (
        ingest.lease_connection() as first,
        ingest.lease_connection() as second,
        ingest.lease_connection() as third,
    ):
        engine.reserve_host(first, "slots.example", 2)
        engine.reserve_host(second, "slots.example", 2)
        with pytest.raises(HostBusyError):
            engine.reserve_host(third, "slots.example", 2)
        engine.reserve_host(third, "other-slots.example", 2)


def test_execution_lock_with_default_slots_is_exclusive(ingest):
    engine = Engine(ingest)
    with ingest.lease_connection() as first, ingest.lease_connection() as second:
        engine.reserve_host(first, "exclusive.example", 1)
        with pytest.raises(HostBusyError):
            engine.reserve_host(second, "exclusive.example", 1)
