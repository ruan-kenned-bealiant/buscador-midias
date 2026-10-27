"""Consistência e retomada de lotes de dedup em PostgreSQL isolado."""

import os
import time

import psycopg
import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest, next_job  # noqa: F401

from buscador_midias.ingestion.runtime import Service

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito"
)


def prepare(repository, count):
    run = repository.create_run({})
    repository.finish(
        next_job(repository, "discovery"),
        dict(candidates=[dict(url=f"https://jose.example/news/{i}") for i in range(count)]),
    )
    with repository.connection() as db:
        ids = [
            r["id"]
            for r in db.execute("SELECT id FROM ingest_jobs WHERE stage='dedup' ORDER BY id")
        ]
    return run, repository.claim_many(ids, "batch-test")


def test_batch_mixed_results_and_replay_are_idempotent(ingest):  # noqa: F811
    _, jobs = prepare(ingest, 3)
    outputs = [
        dict(next="fetch", outcome="nova_url"),
        dict(status="known", outcome="existente", article_id=42),
        dict(status="outside_window", outcome="fora_da_janela", article=dict(date_basis="xml")),
    ]
    ingest.finish_many(jobs, outputs)
    ingest.finish_many(jobs, outputs)
    with ingest.connection() as db:
        assert (
            db.execute("SELECT count(*) n FROM ingest_jobs WHERE stage='fetch'").fetchone()["n"]
            == 1
        )
        assert (
            db.execute(
                "SELECT count(*) n FROM ingest_jobs WHERE stage='dedup' AND state='succeeded'"
            ).fetchone()["n"]
            == 3
        )
        assert (
            db.execute("SELECT count(*) n FROM ingest_outbox WHERE stage='fetch'").fetchone()["n"]
            == 1
        )
        assert (
            db.execute(
                "SELECT article_id FROM ingest_articles WHERE hash=%s", (jobs[1]["hash"],)
            ).fetchone()["article_id"]
            == 42
        )
        assert (
            db.execute(
                "SELECT payload->>'date_basis' basis FROM ingest_articles WHERE hash=%s",
                (jobs[2]["hash"],),
            ).fetchone()["basis"]
            == "xml"
        )


@pytest.mark.parametrize(
    "column,value", [("owner", "new-owner"), ("dispatch", 99), ("attempts", 99)]
)
def test_batch_ignores_superseded_reservation(ingest, column, value):  # noqa: F811
    _, jobs = prepare(ingest, 1)
    with ingest.connection() as db:
        db.execute(
            psycopg.sql.SQL("UPDATE ingest_jobs SET {}=%s WHERE id=%s").format(
                psycopg.sql.Identifier(column)
            ),
            (value, jobs[0]["id"]),
        )
    ingest.finish_many(jobs, [dict(next="fetch")])
    with ingest.connection() as db:
        assert (
            db.execute("SELECT count(*) n FROM ingest_jobs WHERE stage='fetch'").fetchone()["n"]
            == 0
        )
        assert (
            db.execute("SELECT state FROM ingest_jobs WHERE id=%s", (jobs[0]["id"],)).fetchone()[
                "state"
            ]
            == "running"
        )


def test_batch_rolls_back_all_results_on_invalid_sql_value(ingest):  # noqa: F811
    _, jobs = prepare(ingest, 2)
    with pytest.raises(psycopg.Error):
        ingest.finish_many(jobs, [dict(next="fetch"), dict(article_id="not-an-integer")])
    with ingest.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) n FROM ingest_jobs WHERE stage='dedup' AND state='running'"
            ).fetchone()["n"]
            == 2
        )
        assert (
            db.execute("SELECT count(*) n FROM ingest_jobs WHERE stage='fetch'").fetchone()["n"]
            == 0
        )


def test_lightweight_stages_do_not_wait_for_download_or_evidence(ingest):  # noqa: F811
    run = ingest.create_run({})
    with ingest.connection() as db:
        poll = db.execute("SELECT id FROM ingest_polls").fetchone()["id"]
        for stage, n in [("fetch", 2001), ("dedup", 5001), ("evidence", 201)]:
            db.execute(
                "INSERT INTO ingest_jobs(id,stage,subject,run_id,poll_id) "
                "SELECT md5(%s||i::text),%s,i::text,%s,%s FROM generate_series(1,%s) i",
                (stage, stage, run["id"], poll, n),
            )
    assert ingest.capacity("discovery") == (True, "")
    assert ingest.capacity("dedup") == (True, "")
    assert ingest.capacity("context") == (True, "")


def test_pool_reuses_connections_and_rolls_back_without_holding_network_leases(ingest):  # noqa: F811
    ingest.start_pool()
    try:
        pids = []
        for _ in range(6):
            with ingest.connection() as db:
                row = db.execute("SELECT pg_backend_pid() pid,current_schema() schema").fetchone()
                pids.append(row["pid"])
                assert row["schema"] == ingest.store.schema
        assert len(set(pids)) < 6
        with pytest.raises(ValueError), ingest.connection() as db:
            db.execute("INSERT INTO metadata(key,value) VALUES('rollback-pool','{}')")
            raise ValueError("rollback")
        with ingest.connection() as db:
            assert not db.execute("SELECT 1 FROM metadata WHERE key='rollback-pool'").fetchone()
        with ingest.lease_connection() as lease, ingest.connection() as short:
            network_pid = lease.execute("SELECT pg_backend_pid() pid").fetchone()["pid"]
            short_pid = short.execute("SELECT pg_backend_pid() pid").fetchone()["pid"]
            assert network_pid != short_pid
            with ingest.publication_connection() as publication:
                publisher_pid = publication.execute("SELECT pg_backend_pid() pid").fetchone()["pid"]
                assert publisher_pid not in (network_pid, short_pid)
        # Even borrowing every short connection leaves confirms independently available.
        from contextlib import ExitStack

        with ExitStack() as stack:
            for _ in range(12):
                stack.enter_context(ingest.connection())
            with ingest.publication_connection() as publication:
                assert publication.execute("SELECT 1 AS n").fetchone()["n"] == 1
    finally:
        ingest.close_pool()


def test_batch_checkpoint_benchmark_preserves_every_url(ingest):  # noqa: F811
    _, jobs = prepare(ingest, 1000)
    old = jobs[:500]
    new = jobs[500:]
    start = time.perf_counter()
    with ingest.connection() as db, db.pipeline():
        for job in old:
            ingest.finish(job, dict(next="fetch", outcome="nova_url"), db=db)
    old_seconds = time.perf_counter() - start
    start = time.perf_counter()
    ingest.finish_many(new, [dict(next="fetch", outcome="nova_url") for _ in new])
    new_seconds = time.perf_counter() - start
    print(f"CHECKPOINT_BENCHMARK count=500 before={old_seconds:.4f}s after={new_seconds:.4f}s")
    with ingest.connection() as db:
        assert (
            db.execute("SELECT count(*) n FROM ingest_jobs WHERE stage='fetch'").fetchone()["n"]
            == 1000
        )
        assert (
            db.execute(
                "SELECT count(*) n FROM ingest_jobs WHERE stage='dedup' AND state='succeeded'"
            ).fetchone()["n"]
            == 1000
        )


@pytest.mark.parametrize("fail_second", [False, True])
def test_publisher_confirms_before_checkpoint_and_preserves_worker_results(
    ingest,  # noqa: F811
    monkeypatch,
    fail_second,
):
    _, jobs = prepare(ingest, 2)
    ingest.finish_many(jobs, [dict(next="fetch") for _ in jobs])
    # Use metadata stage to publish both messages independently of host admission.
    with ingest.connection() as db:
        db.execute("UPDATE ingest_outbox SET stage='persist' WHERE stage='fetch'")
        db.execute("UPDATE ingest_jobs SET stage='persist' WHERE stage='fetch'")
    calls = []

    def publish(channel, stage, ident, **kwargs):
        calls.append(ident)
        if fail_second and len(calls) == 2:
            raise OSError("broker confirm interrupted")
        # Confirms may reach a worker before the publisher commits its checkpoint.
        # This separate connection must never wait for a job lock held by publishing.
        job = ingest.claim(ident, "fast-worker")
        assert job is not None
        ingest.finish(job, dict(outcome="persisted"))

    monkeypatch.setattr("buscador_midias.ingestion.broker.publish", publish)
    if fail_second:
        with pytest.raises(OSError):
            Service(ingest).publish_once(None, "persist")
    else:
        assert Service(ingest).publish_once(None, "persist") == 2
    with ingest.connection() as db:
        events = db.execute(
            "SELECT published_at FROM ingest_outbox WHERE stage='persist'"
        ).fetchall()
        assert all(bool(e["published_at"]) is not fail_second for e in events)
        assert (
            db.execute("SELECT state FROM ingest_jobs WHERE id=%s", (calls[0],)).fetchone()["state"]
            == "succeeded"
        )
