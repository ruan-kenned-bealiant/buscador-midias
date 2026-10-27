# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Parada de rodadas: cancela o trabalho não analisado e libera os artigos para a próxima rodada."""

from __future__ import annotations

import os
import uuid

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest, next_job  # noqa: F401
from test_ingestion_fast_batch import prepare

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def to_fetch(ingest, count=2):
    """Rodada com ``count`` artigos aguardando download (tarefas de fetch pendentes)."""
    run, jobs = prepare(ingest, count)
    ingest.finish_many(jobs, [dict(next="fetch", outcome="nova_url")] * count)
    return run


def rows(ingest, sql):
    with ingest.connection() as db:
        return db.execute(sql).fetchall()


def add_job(ingest, stage, state):
    """Tarefa avulsa de ``stage`` sobre o primeiro artigo, para etapas que o teste não alcança."""
    with ingest.connection() as db:
        article = db.execute(
            "SELECT a.hash,a.poll_id,a.run_id FROM ingest_articles a ORDER BY a.hash LIMIT 1"
        ).fetchone()
        ident = uuid.uuid4().hex
        db.execute(
            "INSERT INTO ingest_jobs(id,stage,subject,hash,run_id,poll_id,state,generation) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,99)",
            (ident, stage, ident, article["hash"], article["run_id"], article["poll_id"], state),
        )
    return ident


def test_stop_cancels_open_work_and_reports_what_it_did(ingest):
    to_fetch(ingest, 2)
    report = ingest.stop_runs()
    assert report["cancelled"] == 2 and report["by_stage"] == {"fetch": 2}
    assert {
        r["state"] for r in rows(ingest, "SELECT state FROM ingest_jobs WHERE stage='fetch'")
    } == {"cancelled"}


def test_stop_keeps_work_that_already_paid_for_the_analysis(ingest):
    to_fetch(ingest, 1)
    evidence, persist = add_job(ingest, "evidence", "pending"), add_job(ingest, "persist", "queued")
    ingest.stop_runs()
    kept = {r["id"]: r["state"] for r in rows(ingest, "SELECT id,state FROM ingest_jobs")}
    assert kept[evidence] == "pending" and kept[persist] == "queued"


def test_stop_does_not_touch_jobs_a_worker_is_running_or_that_already_finished(ingest):
    to_fetch(ingest, 1)
    running = add_job(ingest, "context", "running")
    ingest.stop_runs()
    states = {r["id"]: r["state"] for r in rows(ingest, "SELECT id,state FROM ingest_jobs")}
    assert states[running] == "running"
    assert {s for i, s in states.items() if i != running} == {"succeeded", "cancelled"}


def test_stop_marks_the_articles_so_the_next_discovery_can_take_them_again(ingest):
    to_fetch(ingest, 2)
    ingest.stop_runs()
    assert {r["status"] for r in rows(ingest, "SELECT status FROM ingest_articles")} == {
        "cancelled"
    }


def test_stop_leaves_articles_that_were_not_interrupted_alone(ingest):
    _, jobs = prepare(ingest, 2)
    ingest.finish_many(
        jobs, [dict(next="fetch", outcome="nova_url"), dict(status="known", outcome="existente")]
    )
    ingest.stop_runs()
    assert sorted(r["status"] for r in rows(ingest, "SELECT status FROM ingest_articles")) == [
        "cancelled",
        "known",
    ]


def test_stop_discards_the_unpublished_messages_of_cancelled_work(ingest):
    to_fetch(ingest, 2)
    ingest.stop_runs()
    pending = rows(
        ingest,
        "SELECT count(*) n FROM ingest_outbox WHERE stage='fetch' AND published_at IS NULL "
        "AND discarded_at IS NULL",
    )
    assert pending[0]["n"] == 0


def test_stop_is_idempotent(ingest):
    to_fetch(ingest, 2)
    ingest.stop_runs()
    assert ingest.stop_runs() == dict(cancelled=0, by_stage={})


def test_stopped_run_closes_as_partial_and_frees_the_links(ingest):
    run = to_fetch(ingest, 2)
    ingest.stop_runs()
    ingest.maintain()
    closed = rows(ingest, f"SELECT state,finished_at FROM ingest_runs WHERE id='{run['id']}'")[0]
    assert closed["state"] == "partial" and closed["finished_at"] is not None
    assert rows(ingest, "SELECT count(*) n FROM ingest_polls WHERE NOT discovery_done")[0]["n"] == 0


def test_next_round_takes_the_interrupted_articles_again(ingest):
    to_fetch(ingest, 2)
    ingest.stop_runs()
    ingest.maintain()
    ingest.create_run({})
    ingest.finish(
        next_job(ingest, "discovery"),
        dict(candidates=[dict(url=f"https://jose.example/news/{i}") for i in range(2)]),
    )
    again = rows(
        ingest, "SELECT generation,state FROM ingest_jobs WHERE stage='dedup' ORDER BY generation"
    )
    assert [r["generation"] for r in again] == [1, 1, 2, 2]
    assert {r["status"] for r in rows(ingest, "SELECT status FROM ingest_articles")} == {"pending"}
