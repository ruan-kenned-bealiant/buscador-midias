"""Manutenção, retry, backpressure e publicação justa por host."""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from buscador_midias.db.connection import connect
from buscador_midias.db.queue import capacity, maintain, reserve_for_publish, retry_failed
from buscador_midias.db.schema import install

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

SINCE = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
UNTIL = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)


@pytest.fixture
def q(db: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    install(db)
    db.execute(
        "INSERT INTO bm_sites(identity,name,url) VALUES('a.example','A','https://a.example')"
    )
    db.execute(
        "INSERT INTO bm_runs(id,type,trigger,status,config,since,until) "
        "VALUES('r1','search','scheduled','running',%s,%s,%s)",
        (Jsonb(dict(max_attempts=3, retry_minutes=1, failed_retry_limit=3)), SINCE, UNTIL),
    )
    db.row_factory = dict_row  # type: ignore[assignment]
    return db


def digest(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def article(db: psycopg.Connection[Any], name: str, host: str = "a.example", **columns: Any) -> str:
    values: dict[str, Any] = dict(
        hash=digest(name),
        url=f"https://{host}/{name}",
        host=host,
        first_run_id="r1",
        run_id="r1",
    )
    values.update(columns)
    site = db.execute("SELECT id FROM bm_sites").fetchone()
    assert site
    values["site_id"] = site["id"]
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    db.execute(f"INSERT INTO bm_articles({names}) VALUES({marks})", tuple(values.values()))
    return str(values["hash"])


def get(db: psycopg.Connection[Any], hash_: str) -> dict[str, Any]:
    found = db.execute("SELECT * FROM bm_articles WHERE hash=%s", (hash_,)).fetchone()
    assert found
    return found


PAST = "2000-01-01T00:00:00Z"


# --- maintain -----------------------------------------------------------------------------------


def test_an_expired_lease_returns_the_article_to_the_recovery_lane(q):
    hash_ = article(q, "a", state="running", owner="w", attempts=1, locked_until=PAST)
    report = maintain(q)
    row = get(q, hash_)
    assert report.expired == 1
    assert (row["state"], row["lane"], row["owner"], row["locked_until"]) == (
        "retry",
        "recovery",
        None,
        None,
    )


def test_an_expired_lease_on_the_last_attempt_fails_the_article(q):
    hash_ = article(q, "a", state="running", owner="w", attempts=3, locked_until=PAST)
    maintain(q)
    row = get(q, hash_)
    assert (row["state"], row["reason"], row["reason_class"]) == (
        "failed",
        "lease_expired",
        "transient",
    )
    assert row["failed_stage"] == "dedup"


def test_a_live_lease_is_left_alone(q):
    hash_ = article(q, "a", state="running", owner="w", attempts=1, locked_until="2999-01-01Z")
    assert maintain(q).expired == 0
    assert get(q, hash_)["state"] == "running"


def test_a_queued_article_whose_message_was_lost_is_published_again(q):
    stale = article(q, "stale", state="queued", queued_at=PAST)
    fresh = article(q, "fresh", state="queued", queued_at="2999-01-01Z")
    report = maintain(q, queued_stale_seconds=900)
    assert report.republished == 1
    assert (get(q, stale)["state"], get(q, stale)["queued_at"]) == ("pending", None)
    assert get(q, fresh)["state"] == "queued"


def test_transient_failures_are_retried_automatically_within_the_pass_limit(q):
    q.execute("UPDATE bm_runs SET status='completed'")
    hash_ = article(
        q,
        "a",
        stage="fetch",
        state="failed",
        failed_stage="fetch",
        reason="http_503",
        reason_class="transient",
        retryable=True,
        failed_at=PAST,
        attempts=3,
        passes=1,
    )
    report = maintain(q, auto_retry_after_seconds=60)
    row = get(q, hash_)
    assert report.auto_retried == 1
    assert (row["state"], row["stage"], row["attempts"], row["passes"]) == ("retry", "fetch", 0, 2)
    assert row["lane"] == "primary"


@pytest.mark.parametrize(
    "columns",
    [
        dict(reason_class="blocked", retryable=False),
        dict(reason_class="permanent", retryable=False),
        dict(reason_class="bug", retryable=False),
        dict(reason_class="transient", retryable=True, passes=3),
        dict(reason_class="transient", retryable=True, failed_at="2999-01-01Z"),
    ],
)
def test_only_recent_enough_transient_failures_under_the_limit_are_retried(q, columns):
    q.execute("UPDATE bm_runs SET status='completed'")
    values = dict(
        state="failed",
        stage="fetch",
        failed_stage="fetch",
        reason="x",
        failed_at=PAST,
        passes=1,
    )
    values.update(columns)
    hash_ = article(q, "a", **values)
    assert maintain(q, auto_retry_after_seconds=60).auto_retried == 0
    assert get(q, hash_)["state"] == "failed"


def test_the_same_round_never_retries_its_own_failure(q):
    hash_ = article(
        q,
        "a",
        state="failed",
        stage="fetch",
        failed_stage="fetch",
        reason_class="transient",
        retryable=True,
        failed_at=PAST,
    )
    assert maintain(q, auto_retry_after_seconds=60).auto_retried == 0
    q.execute("UPDATE bm_runs SET status='partial'")
    assert maintain(q, auto_retry_after_seconds=60).auto_retried == 1
    assert get(q, hash_)["state"] == "retry"


def test_the_default_run_limit_of_one_pass_never_retries_automatically(q):
    q.execute("UPDATE bm_runs SET status='completed'")
    q.execute("UPDATE bm_runs SET config=%s", (Jsonb(dict(max_attempts=3)),))
    article(
        q,
        "a",
        state="failed",
        failed_stage="dedup",
        reason_class="transient",
        retryable=True,
        failed_at=PAST,
    )
    assert maintain(q, auto_retry_after_seconds=60).auto_retried == 0


# --- retry manual -------------------------------------------------------------------------------


def failed(q, name: str, **columns: Any) -> str:
    values: dict[str, Any] = dict(
        state="failed",
        stage="fetch",
        failed_stage="fetch",
        reason="http_403",
        reason_class="blocked",
        retryable=False,
        failed_at=PAST,
        attempts=1,
    )
    values.update(columns)
    return article(q, name, **values)


def test_manual_retry_reopens_what_the_filter_selects(q):
    blocked = failed(q, "blk")
    gone = failed(q, "gone", reason="http_404", reason_class="permanent")
    other_stage = failed(
        q, "ctx", stage="context", failed_stage="context", reason_class="bug", reason="internal"
    )
    assert retry_failed(q, reason_class="blocked") == 1
    assert get(q, blocked)["state"] == "retry" and get(q, gone)["state"] == "failed"
    assert retry_failed(q, stage="context") == 1
    assert get(q, other_stage)["stage"] == "context" and get(q, other_stage)["passes"] == 2


def test_manual_retry_by_reason_run_and_explicit_hashes(q):
    q.execute(
        "INSERT INTO bm_runs(id,type,trigger,status,config) "
        "VALUES('r2','search','manual','completed','{}')"
    )
    one = failed(q, "one")
    two = failed(q, "two", reason="http_401", run_id="r2")
    assert retry_failed(q, reason="http_401") == 1
    assert get(q, two)["state"] == "retry" and get(q, one)["state"] == "failed"
    assert retry_failed(q, run_id="r1") == 1
    three = failed(q, "three")
    assert retry_failed(q, hashes=[three]) == 1


def test_manual_retry_ignores_pass_limits_and_resets_attempts(q):
    hash_ = failed(q, "a", passes=9)
    retry_failed(q)
    row = get(q, hash_)
    assert (row["attempts"], row["passes"], row["lane"]) == (0, 10, "primary")
    assert row["next_attempt_at"] <= row["updated_at"]


def test_manual_retry_only_touches_failed_articles(q):
    running = article(q, "run", state="running", owner="w", locked_until="2999-01-01Z")
    assert retry_failed(q) == 0
    assert get(q, running)["state"] == "running"


def test_manual_retry_respects_the_limit_argument(q):
    for name in ("a", "b", "c"):
        failed(q, name)
    assert retry_failed(q, limit=2) == 2


# --- capacity ---------------------------------------------------------------------------------


def test_capacity_counts_the_downstream_backlog(q):
    limits = {"context": 2}
    assert capacity(q, "fetch", limits)
    article(q, "a", stage="context", state="pending")
    assert capacity(q, "fetch", limits)
    article(q, "b", stage="context", state="running", owner="w", locked_until="2999-01-01Z")
    assert not capacity(q, "fetch", limits)
    assert capacity(q, "dedup", limits)
    assert capacity(q, "context", limits)


def test_capacity_ignores_recovery_and_finished_articles(q):
    limits = {"context": 1}
    article(q, "a", stage="context", state="pending", lane="recovery")
    article(q, "b", stage="context", state="done", outcome="no_entities")
    article(q, "c", stage="context", state="failed", failed_stage="context")
    assert capacity(q, "fetch", limits)


# --- publicar -----------------------------------------------------------------------------------


def test_publication_marks_the_oldest_waiting_articles_as_queued(q):
    old = article(q, "old", next_attempt_at="2026-01-01Z")
    newer = article(q, "newer", next_attempt_at="2026-02-01Z")
    article(q, "later", next_attempt_at="2999-01-01Z")
    article(q, "busy", state="running", owner="w", locked_until="2999-01-01Z")
    chosen = reserve_for_publish(q, "dedup", "primary", limit=10)
    assert chosen == [old, newer]
    row = get(q, old)
    assert row["state"] == "queued" and row["queued_at"] is not None
    assert reserve_for_publish(q, "dedup", "primary", limit=10) == []


def test_publication_honours_stage_lane_and_limit(q):
    article(q, "f", stage="fetch")
    article(q, "r", lane="recovery")
    for n in range(5):
        article(q, f"p{n}")
    assert len(reserve_for_publish(q, "dedup", "primary", limit=3)) == 3
    assert len(reserve_for_publish(q, "dedup", "recovery", limit=3)) == 1
    assert len(reserve_for_publish(q, "fetch", "primary", limit=3)) == 1


def test_publication_with_no_room_reserves_nothing(q):
    article(q, "a")
    assert reserve_for_publish(q, "dedup", "primary", limit=0) == []
    assert get(q, digest("a"))["state"] == "pending"


def test_hosts_take_turns_so_one_big_site_cannot_starve_the_others(q):
    for n in range(6):
        article(q, f"big{n}", host="big.example", next_attempt_at=f"2026-01-0{n + 1}Z")
    small = article(q, "small", host="small.example", next_attempt_at="2026-03-01Z")
    other = article(q, "other", host="other.example", next_attempt_at="2026-03-02Z")
    chosen = reserve_for_publish(q, "dedup", "primary", limit=4, per_host=1)
    hosts = [get(q, h)["host"] for h in chosen]
    assert sorted(hosts) == ["big.example", "other.example", "small.example"]
    assert small in chosen and other in chosen


def test_per_host_limit_counts_what_is_already_queued_or_running(q):
    article(q, "run", host="a.example", state="running", owner="w", locked_until="2999-01-01Z")
    article(q, "que", host="a.example", state="queued", queued_at="2999-01-01Z")
    waiting = article(q, "wait", host="a.example")
    free = article(q, "free", host="b.example")
    assert reserve_for_publish(q, "dedup", "primary", limit=10, per_host=2) == [free]
    assert get(q, waiting)["state"] == "pending"
    assert reserve_for_publish(q, "dedup", "primary", limit=10, per_host=3) == [waiting]


def test_hosts_in_cooldown_are_skipped(q):
    q.execute(
        "INSERT INTO bm_host_state(host,cooldown_until) "
        "VALUES('slow.example',now()+interval '1 hour')"
    )
    article(q, "slow", host="slow.example")
    ok = article(q, "ok", host="ok.example")
    assert reserve_for_publish(q, "dedup", "primary", limit=10) == [ok]
    q.execute("UPDATE bm_host_state SET cooldown_until=now()-interval '1 second'")
    assert len(reserve_for_publish(q, "dedup", "primary", limit=10)) == 1


def test_two_publishers_never_choose_the_same_article(q, schema_name):
    for n in range(20):
        article(q, f"n{n}")
    q.commit()

    def publish(_: int) -> list[str]:
        with connect(schema_name) as other:
            other.row_factory = dict_row  # type: ignore[assignment]
            return reserve_for_publish(other, "dedup", "primary", limit=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(publish, [1, 2])
    assert not set(first) & set(second)
    assert len(first) + len(second) >= 10


def test_hosts_with_a_long_backlog_earn_extra_slots_up_to_the_ceiling(q):
    for n in range(250):
        article(q, f"big{n}", host="big.example", next_attempt_at="2026-01-01Z")
    for n in range(5):
        article(q, f"small{n}", host="small.example", next_attempt_at="2026-01-01Z")
    chosen = reserve_for_publish(
        q, "dedup", "primary", limit=50, per_host=1, per_host_max=4, backlog_step=100
    )
    hosts = [get(q, h)["host"] for h in chosen]
    # 250 em espera: 1 + 250//100 = 3 vagas; o site pequeno continua com a vaga base
    assert hosts.count("big.example") == 3 and hosts.count("small.example") == 1


def test_the_ceiling_caps_the_extra_slots(q):
    for n in range(900):
        article(q, f"big{n}", host="big.example")
    chosen = reserve_for_publish(
        q, "dedup", "primary", limit=50, per_host=1, per_host_max=2, backlog_step=100
    )
    assert len(chosen) == 2
