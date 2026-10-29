"""Reservar, concluir e falhar uma etapa: compare-and-set sobre a linha da notícia."""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from buscador_midias.db.connection import connect
from buscador_midias.db.queue import claim, complete, fail
from buscador_midias.db.schema import install

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

SINCE = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
UNTIL = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
RUN_CONFIG = dict(max_attempts=3, retry_minutes=1)


@pytest.fixture
def q(db: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    install(db)
    db.execute("INSERT INTO bm_mechanisms(model,parameters) VALUES('rss','{}')")
    db.execute(
        "INSERT INTO midias_ia_trusted_sources(identity,name,url) VALUES('a.example','A','https://a.example')"
    )
    db.execute(
        "INSERT INTO bm_runs(id,type,trigger,status,config,since,until) "
        "VALUES('r1','search','scheduled','running',%s,%s,%s)",
        (psycopg.types.json.Jsonb(RUN_CONFIG), SINCE, UNTIL),
    )
    db.row_factory = dict_row  # type: ignore[assignment]
    return db


def article(db: psycopg.Connection[Any], name: str = "n1", **columns: Any) -> str:
    values: dict[str, Any] = dict(
        hash=hashlib.sha256(name.encode()).hexdigest(),
        url=f"https://a.example/{name}",
        host="a.example",
        first_run_id="r1",
        run_id="r1",
    )
    values.update(columns)
    site = db.execute("SELECT id FROM midias_ia_trusted_sources").fetchone()
    assert site
    values["site_id"] = site["id"]
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    db.execute(f"INSERT INTO bm_articles({names}) VALUES({marks})", tuple(values.values()))
    return str(values["hash"])


def current(db: psycopg.Connection[Any], hash_: str) -> dict[str, Any]:
    found = db.execute("SELECT * FROM bm_articles WHERE hash=%s", (hash_,)).fetchone()
    assert found
    return found


def taken(
    db: psycopg.Connection[Any], name: str = "n1", stage: str = "dedup", **columns: Any
) -> dict[str, Any]:
    hash_ = article(db, name, **columns)
    jobs = claim(db, stage, [hash_], "worker-1")
    assert len(jobs) == 1
    return jobs[0]


# --- claim --------------------------------------------------------------------------------------


def test_claim_moves_the_article_to_running_with_a_lease(q):
    hash_ = article(q)
    jobs = claim(q, "dedup", [hash_], "worker-1")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["hash"] == job["id"] == hash_
    assert job["stage"] == "dedup" and job["owner"] == "worker-1" and job["attempts"] == 1
    assert job["run"]["id"] == "r1" and job["run"]["config"]["max_attempts"] == 3
    row = current(q, hash_)
    assert row["state"] == "running" and row["locked_until"] > row["updated_at"]


def test_a_duplicate_or_stale_message_claims_nothing(q):
    hash_ = article(q)
    assert claim(q, "dedup", [hash_], "worker-1")
    assert claim(q, "dedup", [hash_], "worker-2") == []
    assert current(q, hash_)["owner"] == "worker-1" and current(q, hash_)["attempts"] == 1


def test_claim_respects_the_retry_schedule_and_the_stage(q):
    later = article(q, "later", next_attempt_at="2999-01-01T00:00:00Z", state="retry")
    wrong = article(q, "wrong", stage="fetch")
    assert claim(q, "dedup", [later, wrong], "w") == []
    assert [j["hash"] for j in claim(q, "fetch", [wrong], "w")] == [wrong]


@pytest.mark.parametrize("state", ["pending", "queued", "retry"])
def test_claim_accepts_waiting_states(q, state):
    hash_ = article(q, state=state)
    assert len(claim(q, "dedup", [hash_], "w")) == 1


@pytest.mark.parametrize("state", ["running", "done", "failed", "cancelled"])
def test_claim_ignores_states_that_are_not_waiting(q, state):
    hash_ = article(q, state=state)
    assert claim(q, "dedup", [hash_], "w") == []


def test_claim_can_be_limited_to_a_lane(q):
    primary = article(q, "p", lane="primary")
    recovery = article(q, "r", lane="recovery")
    assert [j["hash"] for j in claim(q, "dedup", [primary, recovery], "w", lane="recovery")] == [
        recovery
    ]
    assert [j["hash"] for j in claim(q, "dedup", [primary, recovery], "w", lane="primary")] == [
        primary
    ]


def test_claim_handles_empty_input(q):
    assert claim(q, "dedup", [], "w") == []


def test_only_one_of_two_concurrent_consumers_wins(q, schema_name):
    hash_ = article(q)
    q.commit()

    def attempt(owner: str) -> int:
        with connect(schema_name) as other:
            other.row_factory = dict_row  # type: ignore[assignment]
            return len(claim(other, "dedup", [hash_], owner))

    with ThreadPoolExecutor(max_workers=2) as pool:
        wins = list(pool.map(attempt, ["a", "b"]))
    assert sorted(wins) == [0, 1]


# --- complete -----------------------------------------------------------------------------------


def test_complete_advances_to_the_next_stage_and_resets_the_queue_fields(q):
    job = taken(q)
    assert complete(q, job, dict(next="fetch", article=dict(title="T")))
    row = current(q, job["hash"])
    assert (row["stage"], row["state"], row["attempts"], row["lane"]) == (
        "fetch",
        "pending",
        0,
        "primary",
    )
    assert row["owner"] is None and row["locked_until"] is None and row["queued_at"] is None
    assert row["payload"]["title"] == "T"


@pytest.mark.parametrize(
    "status",
    [
        "known",
        "outside_window",
        "date_unknown",
        "no_text",
        "excluded_content",
        "no_entities",
        "persisted",
        "evidence_pending",
    ],
)
def test_complete_with_a_terminal_status_closes_the_article(q, status):
    job = taken(q)
    assert complete(q, job, dict(status=status, article_id=77))
    row = current(q, job["hash"])
    assert (row["state"], row["outcome"], row["corpus_article_id"]) == ("done", status, 77)
    assert row["owner"] is None and row["locked_until"] is None


def test_complete_merges_the_patch_without_losing_other_keys(q):
    job = taken(q, payload=psycopg.types.json.Jsonb(dict(title="T", body="B")))
    complete(q, job, dict(next="fetch", article=dict(body=None, language="pt")))
    payload = current(q, job["hash"])["payload"]
    assert payload == dict(title="T", body=None, language="pt")


@pytest.mark.parametrize("status", ["persisted", "known"])
def test_what_the_corpus_already_holds_leaves_the_article_when_it_is_done(q, status):
    """Corpo, análise e evidência de uma notícia concluída vivem no corpus: não se duplicam."""
    heavy = dict(body="B" * 500, analysis=dict(finding=False), evidence=dict(status="captured"))
    job = taken(
        q, payload=psycopg.types.json.Jsonb(dict(title="T", published_at="2026-10-29", **heavy))
    )
    assert complete(q, job, dict(status=status, article_id=9, article=dict(description="D")))
    payload = current(q, job["hash"])["payload"]
    assert payload == dict(title="T", published_at="2026-10-29")


def test_a_discarded_article_keeps_its_payload_for_the_investigation(q):
    job = taken(q, payload=psycopg.types.json.Jsonb(dict(title="T", body="B")))
    assert complete(q, job, dict(status="no_entities", article=dict(analysis=dict(finding=False))))
    payload = current(q, job["hash"])["payload"]
    assert payload["body"] == "B" and payload["analysis"] == dict(finding=False)


def test_a_text_with_a_nul_character_is_stored_without_it(q):
    """O Postgres recusa o NUL em JSONB; uma página assim não pode virar falha interna."""
    job = taken(q)
    assert complete(q, job, dict(next="fetch", article=dict(title="Ti\x00tulo", body="a\x00b")))
    payload = current(q, job["hash"])["payload"]
    assert payload["title"] == "Titulo" and payload["body"] == "ab"


def test_a_failure_detail_with_a_nul_character_is_stored_without_it(q):
    job = taken(q)
    assert fail(q, job, "Erro\x00 estranho") in ("retry", "failed")
    assert "\x00" not in (current(q, job["hash"])["detail"] or "")


def test_a_stale_owner_or_attempt_cannot_complete(q):
    job = taken(q)
    assert not complete(q, {**job, "owner": "other"}, dict(next="fetch"))
    assert not complete(q, {**job, "attempts": 9}, dict(next="fetch"))
    assert not complete(q, {**job, "stage": "fetch"}, dict(next="fetch"))
    assert current(q, job["hash"])["state"] == "running"
    assert complete(q, job, dict(next="fetch"))
    assert not complete(q, job, dict(next="context"))


def test_complete_requires_a_next_stage_or_a_terminal_status(q):
    job = taken(q)
    with pytest.raises(ValueError):
        complete(q, job, dict(article=dict(x=1)))
    with pytest.raises(ValueError):
        complete(q, job, dict(status="weird"))


def test_cost_accumulates_per_stage_and_history_is_capped(q):
    job = taken(q)
    complete(q, job, dict(next="fetch", metrics=dict(cost_usd=0.5, tokens=10, host="x", flag=True)))
    again = claim(q, "fetch", [job["hash"]], "w")[0]
    complete(q, again, dict(next="context", metrics=dict(cost_usd=0.25)))
    row = current(q, job["hash"])
    assert row["cost"]["dedup"] == dict(cost_usd=0.5, tokens=10)
    assert row["cost"]["fetch"] == dict(cost_usd=0.25)
    assert [h["stage"] for h in row["history"]] == ["dedup", "fetch"]
    for _ in range(14):
        row_job = claim(q, "context", [job["hash"]], "w")[0]
        complete(q, row_job, dict(next="context"))
    assert len(current(q, job["hash"])["history"]) == 10


def test_complete_clears_an_earlier_failure(q):
    job = taken(q, state="retry", reason="http_503", reason_class="transient")
    complete(q, job, dict(status="persisted"))
    row = current(q, job["hash"])
    assert row["reason"] is None and row["reason_class"] is None and row["failed_at"] is None


# --- fail ---------------------------------------------------------------------------------------


def test_a_transient_failure_retries_with_exponential_backoff(q):
    job = taken(q)
    assert fail(q, job, "HTTP 503 no XML") == "retry"
    first = current(q, job["hash"])
    assert first["state"] == "retry" and first["owner"] is None
    delay = (first["next_attempt_at"] - first["updated_at"]).total_seconds()
    assert 55 <= delay <= 65
    second = claim_ready(q, job["hash"])
    fail(q, second, "HTTP 503 no XML")
    again = current(q, job["hash"])
    assert 115 <= (again["next_attempt_at"] - again["updated_at"]).total_seconds() <= 125


def claim_ready(db: psycopg.Connection[Any], hash_: str) -> dict[str, Any]:
    db.execute("UPDATE bm_articles SET next_attempt_at=now() WHERE hash=%s", (hash_,))
    return claim(db, "dedup", [hash_], "worker-1")[0]


def test_recovery_failures_move_to_the_recovery_lane(q):
    job = taken(q)
    fail(q, job, "HTTP 503", recovery=True)
    assert current(q, job["hash"])["lane"] == "recovery"


def test_busy_hosts_do_not_consume_an_attempt(q):
    job = taken(q)
    assert fail(q, job, "Host aguardando Retry-After", busy=True, defer_seconds=30) == "retry"
    row = current(q, job["hash"])
    assert row["attempts"] == 0 and row["state"] == "retry"
    assert 25 <= (row["next_attempt_at"] - row["updated_at"]).total_seconds() <= 35


def test_the_last_attempt_fails_the_article_with_its_classification(q):
    hash_ = article(q)
    for _ in range(3):
        job = claim_ready(q, hash_)
        outcome = fail(q, job, "HTTP 503 no XML")
    assert outcome == "failed"
    row = current(q, hash_)
    assert row["state"] == "failed" and row["failed_stage"] == "dedup"
    assert (row["reason"], row["reason_class"], row["retryable"]) == (
        "http_503",
        "transient",
        True,
    )
    assert row["failed_at"] is not None and "503" in row["detail"] and row["attempts"] == 3


def test_a_permanent_reason_fails_at_once_and_blocked_is_not_retryable(q):
    gone = taken(q, "gone")
    assert fail(q, gone, "HTTP 404 no XML") == "failed"
    assert current(q, gone["hash"])["reason_class"] == "permanent"
    blocked = taken(q, "blk")
    assert fail(q, blocked, "HTTP 403 no XML", retryable=False) == "failed"
    row = current(q, blocked["hash"])
    assert (row["reason_class"], row["retryable"]) == ("blocked", False)


def test_failure_by_a_stale_owner_is_ignored(q):
    job = taken(q)
    assert fail(q, {**job, "owner": "other"}, "HTTP 503") == "stale"
    assert current(q, job["hash"])["state"] == "running"


def test_retry_after_sets_the_delay_and_a_host_cooldown(q):
    job = taken(q)
    fail(q, job, "HTTP 429", metrics=dict(retry_after_seconds=900, retry_after_host="a.example"))
    row = current(q, job["hash"])
    assert (row["next_attempt_at"] - row["updated_at"]).total_seconds() >= 895
    cool = q.execute("SELECT cooldown_until FROM bm_host_state WHERE host='a.example'").fetchone()
    assert cool and cool["cooldown_until"] > row["updated_at"]


def test_history_records_failures_too(q):
    job = taken(q)
    fail(q, job, "HTTP 503 no XML")
    last = current(q, job["hash"])["history"][-1]
    assert last["stage"] == "dedup" and last["outcome"] == "retry" and "503" in last["detail"]


# --- complete_many -------------------------------------------------------------------------------


def test_a_batch_applies_each_outcome_in_one_transaction(q):
    from buscador_midias.db.queue import complete_many

    names = ["a", "b", "c"]
    keys = [article(q, n) for n in names]
    jobs = claim(q, "dedup", keys, "w1")
    wanted = {
        keys[0]: dict(status="known", article_id=7),
        keys[1]: dict(status="outside_window", article=dict(published_at="2020-01-01T00:00:00Z")),
        keys[2]: dict(next="fetch", metrics=dict(requests=2)),
    }
    assert complete_many(q, jobs, [wanted[j["hash"]] for j in jobs]) == 3
    a, b, c = (current(q, k) for k in keys)
    assert (a["state"], a["outcome"], a["corpus_article_id"]) == ("done", "known", 7)
    assert (b["state"], b["outcome"]) == ("done", "outside_window")
    assert b["payload"]["published_at"] == "2020-01-01T00:00:00Z"
    assert (c["state"], c["stage"], c["attempts"], c["cost"]["dedup"]) == (
        "pending",
        "fetch",
        0,
        dict(requests=2),
    )
    assert all(r["owner"] is None and r["history"][-1]["outcome"] == "ok" for r in (a, b, c))


def test_a_batch_skips_stale_reservations_and_reports_how_many_applied(q):
    from buscador_midias.db.queue import complete_many

    keys = [article(q, n) for n in ("a", "b")]
    jobs = claim(q, "dedup", keys, "w1")
    jobs[1] = {**jobs[1], "owner": "someone-else"}
    assert complete_many(q, jobs, [dict(next="fetch"), dict(next="fetch")]) == 1
    assert {current(q, k)["stage"] for k in keys} == {"dedup", "fetch"}


def test_an_invalid_output_rejects_the_whole_batch(q):
    from buscador_midias.db.queue import complete_many

    keys = [article(q, n) for n in ("a", "b")]
    jobs = claim(q, "dedup", keys, "w1")
    with pytest.raises(ValueError):
        complete_many(q, jobs, [dict(next="fetch"), dict(article=dict(x=1))])
    assert {current(q, k)["state"] for k in keys} == {"running"}


def test_an_empty_batch_is_a_noop(q):
    from buscador_midias.db.queue import complete_many

    assert complete_many(q, [], []) == 0


def test_the_history_of_a_batched_article_stays_capped(q):
    from buscador_midias.db.queue import complete_many

    key = article(q, "a")
    for _ in range(14):
        job = claim_ready(q, key)
        complete_many(q, [job], [dict(next="dedup")])
    assert len(current(q, key)["history"]) == 10


def test_a_large_batch_completes_quickly(q):
    import time

    from buscador_midias.db.queue import complete_many

    keys = [article(q, f"n{n}") for n in range(500)]
    jobs = claim(q, "dedup", keys, "w1")
    started = time.perf_counter()
    assert complete_many(q, jobs, [dict(next="fetch")] * 500) == 500
    assert time.perf_counter() - started < 5


# --- renovar a reserva -------------------------------------------------------------------------


def test_renewing_extends_the_lease_of_the_current_owner(q):
    from buscador_midias.db.queue import renew_lease

    job = taken(q)
    q.execute("UPDATE bm_articles SET locked_until=now()+interval '5 seconds'")
    assert renew_lease(q, job, seconds=600)
    row = current(q, job["hash"])
    assert (row["locked_until"] - row["updated_at"]).total_seconds() > 500


def test_a_stale_owner_or_attempt_cannot_renew(q):
    from buscador_midias.db.queue import renew_lease

    job = taken(q)
    assert not renew_lease(q, {**job, "owner": "other"})
    assert not renew_lease(q, {**job, "attempts": 9})
    complete(q, job, dict(next="fetch"))
    assert not renew_lease(q, job)


def test_renewal_also_works_for_discovery_tasks(q):
    from buscador_midias.db.queue import DISCOVERY, renew_lease

    q.execute(
        "INSERT INTO bm_mechanisms(model,parameters) VALUES('rss','{}') ON CONFLICT DO NOTHING"
    )
    q.execute(
        "INSERT INTO bm_links(site_id,url,kind,mechanism) VALUES(1,'https://a.example/f','feed','rss')"
    )
    q.execute("INSERT INTO bm_run_links(run_id,link_id) VALUES('r1',1)")
    q.execute(
        "INSERT INTO bm_discovery_tasks(run_id,link_id,url,host) "
        "VALUES('r1',1,'https://a.example/f','a.example')"
    )
    ident = q.execute("SELECT id FROM bm_discovery_tasks").fetchone()["id"]
    job = claim(q, "discovery", [ident], "w1", queue=DISCOVERY)[0]
    assert renew_lease(q, job, queue=DISCOVERY)
