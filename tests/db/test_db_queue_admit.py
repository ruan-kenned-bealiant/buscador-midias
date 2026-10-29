"""Admissão das notícias descobertas: nova, já conhecida, reaberta e o que nunca reabre."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from buscador_midias.db.queue import admit
from buscador_midias.db.schema import install
from buscador_midias.domain.url import url_hash

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

SINCE = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
UNTIL = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
CONFIG = dict(allow_undated=False)


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
    for ident in ("r1", "r2"):
        db.execute(
            "INSERT INTO bm_runs(id,type,trigger,status,config,since,until) "
            "VALUES(%s,'search','scheduled','running','{}',%s,%s)",
            (ident, SINCE, UNTIL),
        )
        db.execute(
            "INSERT INTO bm_run_links(run_id,link_id,site_id) "
            "SELECT %s,l.id,l.site_id FROM bm_links l",
            (ident,),
        )
    db.row_factory = dict_row  # type: ignore[assignment]
    return db


def ctx(db: psycopg.Connection[Any], run_id: str = "r1", **config: Any) -> dict[str, Any]:
    link = db.execute("SELECT id,site_id FROM bm_links").fetchone()
    assert link
    return dict(
        run=dict(id=run_id, since=SINCE, until=UNTIL, config={**CONFIG, **config}),
        link=link,
        document_url="https://a.example/feed",
    )


def cand(n: int, **extra: Any) -> dict[str, Any]:
    return dict(url=f"https://a.example/n/{n}", **extra)


def row(db: psycopg.Connection[Any], n: int) -> dict[str, Any]:
    found = db.execute(
        "SELECT * FROM bm_articles WHERE hash=%s", (url_hash(f"https://a.example/n/{n}"),)
    ).fetchone()
    assert found
    return found


def set_article(db: psycopg.Connection[Any], n: int, **columns: Any) -> None:
    assignments = ",".join(f"{k}=%s" for k in columns)
    db.execute(
        f"UPDATE bm_articles SET {assignments} WHERE hash=%s",
        [*columns.values(), url_hash(f"https://a.example/n/{n}")],
    )


def test_new_candidates_enter_pending_at_dedup_with_their_origin(q):
    result = admit(q, [cand(1, title="x"), cand(2)], **ctx(q))
    assert (result.new, result.known, result.reopened) == (2, 0, 0)
    article = row(q, 1)
    assert article["stage"] == "dedup" and article["state"] == "pending"
    assert article["host"] == "a.example" and article["seen_count"] == 1
    assert article["first_run_id"] == article["run_id"] == "r1"
    assert article["document_url"] == "https://a.example/feed"
    assert article["payload"]["title"] == "x"
    assert article["link_id"] is not None and article["site_id"] is not None


def test_duplicates_inside_one_batch_collapse(q):
    result = admit(q, [cand(1), cand(1), cand(1, title="ultimo")], **ctx(q))
    assert result.new == 1
    assert q.execute("SELECT count(*) AS n FROM bm_articles").fetchone()["n"] == 1


def test_an_empty_batch_is_a_noop(q):
    result = admit(q, [], **ctx(q))
    assert (result.new, result.known, result.reopened) == (0, 0, 0)


def test_relisting_counts_the_sighting_without_touching_the_article(q):
    admit(q, [cand(1, title="original")], **ctx(q))
    first_seen = row(q, 1)["last_seen_at"]
    result = admit(q, [cand(1, title="novo")], **ctx(q, "r2"))
    assert (result.new, result.known, result.reopened) == (0, 1, 0)
    article = row(q, 1)
    assert article["seen_count"] == 2 and article["last_seen_at"] >= first_seen
    assert article["state"] == "pending" and article["run_id"] == "r1"
    assert article["payload"]["title"] == "original"


def test_run_link_counters_follow_the_admission(q):
    admit(q, [cand(1), cand(2)], **ctx(q))
    admit(q, [cand(2), cand(3)], **ctx(q, "r2"))
    r1 = q.execute("SELECT * FROM bm_run_links WHERE run_id='r1'").fetchone()
    r2 = q.execute("SELECT * FROM bm_run_links WHERE run_id='r2'").fetchone()
    assert (r1["listed"], r1["new_articles"], r1["known_articles"]) == (2, 2, 0)
    assert (r2["listed"], r2["new_articles"], r2["known_articles"]) == (2, 1, 1)


def test_outside_window_reopens_when_the_publication_date_is_inside(q):
    admit(q, [cand(1)], **ctx(q))
    set_article(
        q,
        1,
        state="done",
        outcome="outside_window",
        payload='{"published_at":"2026-10-29T08:00:00Z"}',
    )
    result = admit(q, [cand(1, title="agora")], **ctx(q, "r2"))
    assert result.reopened == 1 and result.known == 0
    article = row(q, 1)
    assert (article["state"], article["stage"], article["outcome"]) == ("pending", "dedup", None)
    assert article["run_id"] == "r2" and article["first_run_id"] == "r1"
    assert article["attempts"] == 0 and article["payload"]["title"] == "agora"


def test_outside_window_stays_when_the_date_is_outside(q):
    admit(q, [cand(1)], **ctx(q))
    set_article(
        q,
        1,
        state="done",
        outcome="outside_window",
        payload='{"published_at":"2026-10-20T08:00:00Z"}',
    )
    result = admit(q, [cand(1)], **ctx(q, "r2"))
    assert (result.reopened, result.known) == (0, 1)
    assert row(q, 1)["outcome"] == "outside_window"


def test_outside_window_reopens_when_a_learned_route_period_overlaps(q):
    admit(q, [cand(1)], **ctx(q))
    set_article(
        q,
        1,
        state="done",
        outcome="outside_window",
        payload='{"route_period_since":"2026-10-01T00:00:00Z","route_period_until":"2026-10-31T00:00:00Z"}',
    )
    assert admit(q, [cand(1)], **ctx(q, "r2")).reopened == 1


def test_date_unknown_reopens_only_when_undated_articles_are_allowed(q):
    admit(q, [cand(1)], **ctx(q))
    set_article(q, 1, state="done", outcome="date_unknown")
    assert admit(q, [cand(1)], **ctx(q, "r2")).reopened == 0
    assert admit(q, [cand(1)], **ctx(q, "r2", allow_undated=True)).reopened == 1


def test_cancelled_articles_are_always_reopened(q):
    admit(q, [cand(1)], **ctx(q))
    set_article(q, 1, state="cancelled")
    assert admit(q, [cand(1)], **ctx(q, "r2")).reopened == 1
    assert row(q, 1)["state"] == "pending"


@pytest.mark.parametrize(
    "columns",
    [
        dict(state="done", outcome="persisted"),
        dict(state="done", outcome="known"),
        dict(state="done", outcome="no_text"),
        dict(state="done", outcome="no_entities"),
        dict(state="failed", reason="http_403", reason_class="blocked", failed_stage="fetch"),
        dict(state="running", stage="context"),
        dict(state="retry", stage="fetch"),
    ],
)
def test_finished_failed_and_in_flight_articles_are_never_reopened_here(q, columns):
    admit(q, [cand(1)], **ctx(q))
    set_article(q, 1, **columns)
    result = admit(q, [cand(1)], **ctx(q, "r2"))
    assert (result.reopened, result.known) == (0, 1)
    assert row(q, 1)["run_id"] == "r1"


def test_a_large_batch_is_admitted_in_one_call(q):
    result = admit(q, [cand(n) for n in range(1200)], **ctx(q))
    assert result.new == 1200
