"""A fila de descoberta usa a mesma mecânica de reserva, falha, manutenção e publicação."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from buscador_midias.db.queue import DISCOVERY, claim, fail, maintain, reserve_for_publish
from buscador_midias.db.schema import install

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

SINCE = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
UNTIL = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)


@pytest.fixture
def q(db: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    install(db)
    db.execute("INSERT INTO bm_mechanisms(model,parameters) VALUES('rss','{}')")
    db.execute(
        "INSERT INTO bm_sites(identity,name,url) VALUES('a.example','A','https://a.example')"
    )
    db.execute(
        "INSERT INTO bm_links(site_id,url,kind,mechanism) "
        "SELECT id,'https://a.example/feed','feed','rss' FROM bm_sites"
    )
    db.execute(
        "INSERT INTO bm_runs(id,type,trigger,status,config,since,until) "
        "VALUES('r1','search','scheduled','running',%s,%s,%s)",
        (Jsonb(dict(max_attempts=3, retry_minutes=1)), SINCE, UNTIL),
    )
    db.execute("INSERT INTO bm_run_links(run_id,link_id) SELECT 'r1',id FROM bm_links")
    db.row_factory = dict_row  # type: ignore[assignment]
    return db


def task(db: psycopg.Connection[Any], url: str = "https://a.example/feed", **columns: Any) -> int:
    link = db.execute("SELECT id FROM bm_links").fetchone()
    assert link
    values: dict[str, Any] = dict(run_id="r1", link_id=link["id"], url=url, host="a.example")
    values.update(columns)
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    row = db.execute(
        f"INSERT INTO bm_discovery_tasks({names}) VALUES({marks}) RETURNING id",
        tuple(values.values()),
    ).fetchone()
    assert row
    return int(row["id"])


def get(db: psycopg.Connection[Any], ident: int) -> dict[str, Any]:
    row = db.execute("SELECT * FROM bm_discovery_tasks WHERE id=%s", (ident,)).fetchone()
    assert row
    return row


def test_a_discovery_task_is_claimed_like_an_article_stage(q):
    ident = task(q)
    jobs = claim(q, "discovery", [ident], "w1", queue=DISCOVERY)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == ident and job["attempts"] == 1 and job["run"]["id"] == "r1"
    assert claim(q, "discovery", [ident], "w2", queue=DISCOVERY) == []
    assert get(q, ident)["state"] == "running"


def test_a_failed_read_backs_off_then_fails_with_its_reason(q):
    ident = task(q)
    job = claim(q, "discovery", [ident], "w1", queue=DISCOVERY)[0]
    assert fail(q, job, "HTTP 503 no XML", queue=DISCOVERY) == "retry"
    assert get(q, ident)["state"] == "retry"
    q.execute("UPDATE bm_discovery_tasks SET next_attempt_at=now()")
    job = claim(q, "discovery", [ident], "w1", queue=DISCOVERY)[0]
    assert fail(q, job, "HTTP 404 no XML", queue=DISCOVERY) == "failed"
    row = get(q, ident)
    assert (row["reason"], row["reason_class"], row["failed_stage"]) == (
        "http_404",
        "permanent",
        "discovery",
    )


def test_a_dead_consumer_loses_its_lease_and_the_task_returns(q):
    ident = task(q, state="running", owner="gone", attempts=1, locked_until="2000-01-01Z")
    report = maintain(q, queue=DISCOVERY)
    assert report.expired == 1
    row = get(q, ident)
    assert (row["state"], row["lane"]) == ("retry", "recovery")


def test_discovery_publication_shares_the_per_host_fairness(q):
    for n in range(4):
        task(q, f"https://a.example/sitemap-{n}.xml", host="big.example")
    other = task(q, "https://b.example/feed.xml", host="b.example")
    chosen = reserve_for_publish(q, "discovery", "primary", limit=5, per_host=1, queue=DISCOVERY)
    assert len(chosen) == 2 and other in chosen
    assert {get(q, i)["state"] for i in chosen} == {"queued"}


def test_automatic_retry_is_not_applied_to_discovery(q):
    q.execute(
        "UPDATE bm_runs SET status='completed', config=%s", (Jsonb(dict(failed_retry_limit=3)),)
    )
    ident = task(
        q,
        state="failed",
        failed_stage="discovery",
        reason_class="transient",
        retryable=True,
        failed_at="2000-01-01Z",
    )
    assert maintain(q, auto_retry_after_seconds=1, queue=DISCOVERY).auto_retried == 0
    assert get(q, ident)["state"] == "failed"
