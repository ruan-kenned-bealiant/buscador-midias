"""Parar as rodadas abertas: cancela só o trabalho que ninguém executa e ainda não foi pago."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from buscador_midias.db.discovery import finish_runs
from buscador_midias.db.queue import admit
from buscador_midias.db.store import WorkStore
from buscador_midias.domain.url import url_hash

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

SINCE = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
UNTIL = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)


@pytest.fixture
def store(schema_name: str) -> WorkStore:
    work = WorkStore(schema_name)
    work.install()
    with work.connection() as db:
        db.execute(
            "INSERT INTO bm_sites(identity,name,url) VALUES('a.example','A','https://a.example')"
        )
        db.execute(
            "INSERT INTO bm_links(site_id,url,kind,mechanism) "
            "VALUES(1,'https://a.example/feed','feed','rss')"
        )
        for ident, status in (("r1", "running"), ("r2", "completed")):
            db.execute(
                "INSERT INTO bm_runs(id,type,trigger,status,config,since,until) "
                "VALUES(%s,'search','scheduled',%s,%s,%s,%s)",
                (ident, status, Jsonb({}), SINCE, UNTIL),
            )
            db.execute(
                "INSERT INTO bm_run_links(run_id,link_id,site_id,discovery_done) VALUES(%s,1,1,%s)",
                (ident, ident == "r2"),
            )
    return work


def article(store: WorkStore, name: str, run: str = "r1", **columns: Any) -> str:
    values: dict[str, Any] = dict(
        hash=url_hash(f"https://a.example/{name}"),
        url=f"https://a.example/{name}",
        host="a.example",
        site_id=1,
        first_run_id=run,
        run_id=run,
    )
    values.update(columns)
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    with store.connection() as db:
        db.execute(f"INSERT INTO bm_articles({names}) VALUES({marks})", tuple(values.values()))
    return str(values["hash"])


def state(store: WorkStore, key: str) -> str:
    with store.connection() as db:
        return str(
            db.execute("SELECT state FROM bm_articles WHERE hash=%s", (key,)).fetchone()["state"]
        )


def test_waiting_work_before_the_paid_stages_is_cancelled(store):
    keys = {
        stage: article(store, stage, stage=stage, state="retry")
        for stage in ("dedup", "fetch", "context")
    }
    with store.connection() as db:
        db.execute(
            "INSERT INTO bm_discovery_tasks(run_id,link_id,url,host) "
            "VALUES('r1',1,'https://a.example/s.xml','a.example')"
        )
    report = store.stop_runs()
    assert report["cancelled"] == 4
    assert report["by_stage"] == dict(dedup=1, fetch=1, context=1, discovery=1)
    assert {state(store, k) for k in keys.values()} == {"cancelled"}
    with store.connection() as db:
        assert db.execute("SELECT state FROM bm_discovery_tasks").fetchone()["state"] == "cancelled"


def test_paid_stages_and_work_in_flight_are_left_alone(store):
    evidence = article(store, "ev", stage="evidence")
    persist = article(store, "pe", stage="persist")
    running = article(
        store, "run", stage="fetch", state="running", owner="w", locked_until="2999-01-01Z"
    )
    done = article(store, "done", state="done", outcome="persisted")
    failed = article(store, "bad", state="failed", failed_stage="fetch")
    assert store.stop_runs()["cancelled"] == 0
    assert [state(store, k) for k in (evidence, persist, running, done, failed)] == [
        "pending",
        "pending",
        "running",
        "done",
        "failed",
    ]


def test_runs_that_are_not_running_are_not_touched(store):
    other = article(store, "old", "r2", stage="fetch")
    assert store.stop_runs()["cancelled"] == 0
    assert state(store, other) == "pending"


def test_a_stopped_run_ends_partial_and_the_next_round_readmits_the_news(store):
    key = article(store, "n1", stage="fetch", state="queued", queued_at="2999-01-01Z")
    store.stop_runs()
    with store.connection() as db:
        db.execute(
            "UPDATE bm_run_links SET discovery_done=true,state='completed' WHERE run_id='r1'"
        )
        assert finish_runs(db) == 1
        assert (
            db.execute("SELECT status FROM bm_runs WHERE id='r1'").fetchone()["status"] == "partial"
        )
        result = admit(
            db,
            [dict(url="https://a.example/n1")],
            run=dict(id="r2", since=SINCE, until=UNTIL, config={}),
            link=dict(id=1, site_id=1),
            document_url="https://a.example/feed",
        )
        assert result.reopened == 1
    assert state(store, key) == "pending"
