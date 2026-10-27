# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Publicador e workers disputam as mesmas tarefas sem travar uns aos outros."""

from __future__ import annotations

import os
import random
import threading

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest, next_job  # noqa: F401

from buscador_midias.ingestion.admission import confirm_published

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

JOBS, ROUNDS, THREADS = 300, 40, 4


def seed(ingest):
    run = ingest.create_run({})
    root = next_job(ingest, "discovery")
    ingest.finish(root, {})
    with ingest.connection() as db:
        db.execute(
            "INSERT INTO ingest_jobs(id,stage,subject,run_id,poll_id,payload) "
            "SELECT md5('lock-'||n),'context','lock-'||n,%s,%s,"
            "jsonb_build_object('url','https://jose.example/'||n) FROM generate_series(1,%s) n",
            (run["id"], root["poll_id"], JOBS),
        )
        db.execute(
            "INSERT INTO ingest_outbox(job_id,dispatch,stage) "
            "SELECT id,dispatch,stage FROM ingest_jobs WHERE subject LIKE 'lock-%%'"
        )
        ids = [
            r["id"] for r in db.execute("SELECT id FROM ingest_jobs WHERE subject LIKE 'lock-%%'")
        ]
        outbox = [
            r["id"] for r in db.execute("SELECT id FROM ingest_outbox WHERE host IS NOT NULL")
        ]
    return ids, outbox


def release(ingest, ids):
    """Devolve as tarefas ao estado inicial travando em ordem fixa, como o código de produção."""
    with ingest.connection() as db:
        db.execute(
            "WITH l AS (SELECT id FROM ingest_jobs WHERE id=ANY(%s) ORDER BY id FOR UPDATE) "
            "UPDATE ingest_jobs j SET state='pending',owner=NULL FROM l WHERE j.id=l.id",
            (ids,),
        )


def test_publisher_confirmation_and_worker_claims_never_deadlock(ingest):
    ids, outbox = seed(ingest)
    errors: list[BaseException] = []
    barrier = threading.Barrier(THREADS * 2)

    def claimer(number):
        rng = random.Random(number)
        barrier.wait()
        for _ in range(ROUNDS):
            batch = rng.sample(ids, 60)
            try:
                jobs = ingest.claim_many(batch, f"worker-{number}")
                release(ingest, [j["id"] for j in jobs])
            except BaseException as error:  # noqa: BLE001 - qualquer falha reprova o teste
                errors.append(error)

    def publisher(number):
        rng = random.Random(100 + number)
        barrier.wait()
        for _ in range(ROUNDS):
            batch = rng.sample(outbox, 60)
            try:
                with ingest.connection() as db:
                    confirm_published(db, batch)
                release(ingest, ids)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

    threads = [threading.Thread(target=claimer, args=(n,)) for n in range(THREADS)]
    threads += [threading.Thread(target=publisher, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    assert [type(e).__name__ for e in errors] == []
