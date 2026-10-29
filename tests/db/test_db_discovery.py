"""Concluir uma descoberta: filhos, continuação, URLs admitidas, fim da leitura e da rodada."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from buscador_midias.db.discovery import (
    close_reads,
    complete_discovery,
    finish_runs,
    read_plan,
    save_plan,
)
from buscador_midias.db.queue import DISCOVERY, claim, fail
from buscador_midias.db.schema import install
from buscador_midias.domain.url import url_hash

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

SINCE = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
UNTIL = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
FEED = "https://a.example/feed"


@pytest.fixture
def q(db: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    install(db)
    db.execute("INSERT INTO bm_mechanisms(model,parameters) VALUES('rss','{}')")
    db.execute(
        "INSERT INTO bm_sites(identity,name,url) VALUES('a.example','A','https://a.example')"
    )
    db.execute(
        "INSERT INTO bm_links(site_id,url,kind,mechanism) VALUES(1,%s,'feed','rss')", (FEED,)
    )
    db.execute(
        "INSERT INTO bm_runs(id,type,trigger,status,config,since,until) "
        "VALUES('r1','search','scheduled','running',%s,%s,%s)",
        (Jsonb(dict(max_attempts=3, retry_minutes=1)), SINCE, UNTIL),
    )
    db.execute(
        "INSERT INTO bm_run_links(run_id,link_id,site_id,host,discovery_done) "
        "VALUES('r1',1,1,'a.example',false)"
    )
    db.row_factory = dict_row  # type: ignore[assignment]
    return db


def task(db: psycopg.Connection[Any], url: str = FEED, **columns: Any) -> int:
    values: dict[str, Any] = dict(run_id="r1", link_id=1, url=url, host="a.example")
    values.update(columns)
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    row = db.execute(
        f"INSERT INTO bm_discovery_tasks({names}) VALUES({marks}) RETURNING id",
        tuple(values.values()),
    ).fetchone()
    assert row
    return int(row["id"])


def take(db: psycopg.Connection[Any], ident: int) -> dict[str, Any]:
    return claim(db, "discovery", [ident], "w1", queue=DISCOVERY)[0]


def rows(db: psycopg.Connection[Any], where: str = "true") -> list[dict[str, Any]]:
    return db.execute(f"SELECT * FROM bm_discovery_tasks WHERE {where} ORDER BY id").fetchall()


def test_the_task_is_closed_with_its_outcome_and_a_light_summary(q):
    job = take(q, task(q))
    output = dict(
        outcome="urls_encontradas",
        candidates=[],
        children=[],
        metrics=dict(requests=1),
        coverage_partial=False,
    )
    assert complete_discovery(q, job, output)
    row = rows(q)[0]
    assert (row["state"], row["outcome"], row["owner"], row["locked_until"]) == (
        "done",
        "urls_encontradas",
        None,
        None,
    )
    assert row["result"]["metrics"] == dict(requests=1)
    assert row["result"]["candidates"] == 0 and row["result"]["children"] == 0


def test_children_become_tasks_one_level_deeper_without_duplicates(q):
    job = take(q, task(q))
    children = ["https://a.example/s1.xml", "https://a.example/s2.xml", "https://a.example/s1.xml"]
    complete_discovery(q, job, dict(children=children))
    found = rows(q, "depth=1")
    assert [r["url"] for r in found] == ["https://a.example/s1.xml", "https://a.example/s2.xml"]
    assert {r["state"] for r in found} == {"pending"} and found[0]["host"] == "a.example"
    assert found[0]["slice"] == 0 and found[0]["run_id"] == "r1" and found[0]["link_id"] == 1


def test_a_stale_owner_cannot_complete_and_nothing_is_created(q):
    job = take(q, task(q))
    assert not complete_discovery(q, {**job, "owner": "other"}, dict(children=["https://x/y.xml"]))
    assert len(rows(q)) == 1 and rows(q)[0]["state"] == "running"


def test_candidates_are_admitted_with_the_origin_of_the_read(q):
    job = take(q, task(q))
    candidates = [dict(url="https://a.example/n/1"), dict(url="https://a.example/n/2")]
    complete_discovery(q, job, dict(candidates=candidates))
    articles = q.execute("SELECT * FROM bm_articles ORDER BY url").fetchall()
    assert [a["url"] for a in articles] == [c["url"] for c in candidates]
    assert {a["document_url"] for a in articles} == {FEED}
    assert {a["site_id"] for a in articles} == {1} and {a["link_id"] for a in articles} == {1}
    link = q.execute("SELECT listed,new_articles FROM bm_run_links").fetchone()
    assert (link["listed"], link["new_articles"]) == (2, 2)


def test_a_plan_is_kept_in_the_task_and_continuations_point_back_to_it(q):
    first = task(q)
    job = take(q, first)
    plan = dict(candidates=[dict(url=f"https://a.example/n/{n}") for n in range(3)], children=[])
    key = save_plan(q, job, plan)
    assert read_plan(q, key) == plan
    complete_discovery(q, job, dict(candidates=[], plan_key=key, continuation=dict(cursor=1)))
    follow = rows(q, "slice=1")[0]
    assert follow["url"] == FEED and follow["plan_ref"] == int(key) and follow["state"] == "pending"
    assert rows(q, f"id={first}")[0]["plan"] is not None


def test_the_plan_is_dropped_when_the_last_slice_finishes(q):
    first = task(q)
    job = take(q, first)
    key = save_plan(q, job, dict(candidates=[], children=[]))
    complete_discovery(q, job, dict(plan_key=key, continuation=dict(cursor=1)))
    last = take(q, rows(q, "slice=1")[0]["id"])
    complete_discovery(q, last, dict(plan_key=key))
    assert rows(q, f"id={first}")[0]["plan"] is None


def test_a_continuation_without_its_plan_is_rejected(q):
    job = take(q, task(q))
    with pytest.raises(ValueError):
        complete_discovery(q, job, dict(continuation=dict(cursor=1)))
    assert rows(q)[0]["state"] == "running"


def test_reading_a_missing_plan_is_an_error(q):
    with pytest.raises(Exception, match="Plano"):
        read_plan(q, "999999")


def test_the_read_closes_only_when_every_task_of_the_link_is_done(q):
    job = take(q, task(q))
    complete_discovery(q, job, dict(children=["https://a.example/s1.xml"]))
    link = q.execute("SELECT state,discovery_done FROM bm_run_links").fetchone()
    assert (link["state"], link["discovery_done"]) == ("running", False)
    child = take(q, rows(q, "depth=1")[0]["id"])
    complete_discovery(q, child, dict(candidates=[]))
    link = q.execute("SELECT state,discovery_done,finished_at FROM bm_run_links").fetchone()
    assert (link["state"], link["discovery_done"]) == ("completed", True)
    assert link["finished_at"] is not None


def test_a_failed_task_closes_the_read_as_an_error(q):
    job = take(q, task(q))
    assert fail(q, job, "HTTP 404 no XML", queue=DISCOVERY) == "failed"
    assert close_reads(q) == 1
    link = q.execute("SELECT state,discovery_done FROM bm_run_links").fetchone()
    assert (link["state"], link["discovery_done"]) == ("error", True)
    assert close_reads(q) == 0


def test_a_read_with_pending_work_stays_open(q):
    task(q)
    assert close_reads(q) == 0
    assert (
        q.execute("SELECT discovery_done FROM bm_run_links").fetchone()["discovery_done"] is False
    )


# --- fechar a rodada ----------------------------------------------------------------------------


def finished_read(q):
    q.execute("UPDATE bm_run_links SET discovery_done=true,state='completed'")


def article(q, name: str, **columns: Any) -> None:
    values: dict[str, Any] = dict(
        hash=url_hash(f"https://a.example/{name}"),
        url=f"https://a.example/{name}",
        host="a.example",
        site_id=1,
        first_run_id="r1",
        run_id="r1",
    )
    values.update(columns)
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    q.execute(f"INSERT INTO bm_articles({names}) VALUES({marks})", tuple(values.values()))


def status(q) -> dict[str, Any]:
    return q.execute("SELECT status,finished_at FROM bm_runs WHERE id='r1'").fetchone()


def test_a_run_with_open_reads_or_open_articles_keeps_running(q):
    assert finish_runs(q) == 0
    finished_read(q)
    article(q, "open", state="running", stage="fetch", owner="w", locked_until="2999-01-01Z")
    assert finish_runs(q) == 0
    assert status(q)["status"] == "running"


def test_a_run_whose_everything_finished_completes(q):
    finished_read(q)
    article(q, "ok", state="done", outcome="persisted")
    article(q, "old", state="done", outcome="outside_window")
    assert finish_runs(q) == 1
    done = status(q)
    assert done["status"] == "completed" and done["finished_at"] is not None


@pytest.mark.parametrize(
    "columns",
    [
        dict(state="failed", failed_stage="fetch", reason="http_403"),
        dict(state="done", outcome="date_unknown"),
        dict(state="done", outcome="evidence_pending"),
        dict(state="cancelled"),
    ],
)
def test_a_run_with_problems_finishes_as_partial(q, columns):
    finished_read(q)
    article(q, "p", **columns)
    finish_runs(q)
    assert status(q)["status"] == "partial"


def test_a_failed_read_makes_the_run_partial(q):
    q.execute("UPDATE bm_run_links SET discovery_done=true,state='error'")
    finish_runs(q)
    assert status(q)["status"] == "partial"


def test_runs_that_are_not_running_are_left_alone(q):
    finished_read(q)
    q.execute("UPDATE bm_runs SET status='cancelled'")
    assert finish_runs(q) == 0
    assert status(q)["status"] == "cancelled"
