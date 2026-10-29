"""DDL do motor: rodadas, leituras por link, tarefas de descoberta e a notícia como centro."""

from __future__ import annotations

import os
from typing import Any

import psycopg
import pytest

from buscador_midias.db.schema import ENGINE_TABLES, install

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

NOW = "now()"


def one(db: psycopg.Connection[Any], query: str, args: tuple[Any, ...] = ()) -> Any:
    row = db.execute(query, args).fetchone()
    assert row is not None
    return row[0]


@pytest.fixture
def ready(db: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    install(db)
    db.execute("INSERT INTO bm_mechanisms(model,parameters) VALUES('rss','{}')")
    db.execute(
        "INSERT INTO midias_ia_trusted_sources(identity,name,url) VALUES('a.example','A','https://a.example')"
    )
    db.execute(
        "INSERT INTO bm_links(site_id,url,kind,mechanism) "
        "SELECT id,'https://a.example/feed','feed','rss' FROM midias_ia_trusted_sources"
    )
    return db


def run(db: psycopg.Connection[Any], ident: str, **extra: Any) -> str:
    values: dict[str, Any] = dict(
        id=ident, type="search", trigger="scheduled", status="running", config="{}"
    )
    values.update(extra)
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    db.execute(f"INSERT INTO bm_runs({names}) VALUES({marks})", tuple(values.values()))
    return ident


def link_id(db: psycopg.Connection[Any]) -> int:
    return int(one(db, "SELECT id FROM bm_links LIMIT 1"))


def test_install_creates_the_engine_tables_with_prefix(db):
    install(db)
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()"
    ).fetchall()
    names = {r[0] for r in rows}
    assert set(ENGINE_TABLES) <= names
    assert {
        "bm_runs",
        "bm_run_links",
        "bm_discovery_tasks",
        "bm_articles",
        "bm_retry_requests",
        "bm_cleanup_runs",
        "bm_host_state",
        "bm_document_cache",
    } == set(ENGINE_TABLES)


@pytest.mark.parametrize(
    "extra",
    [
        dict(type="export"),
        dict(trigger="cron"),
        dict(status="paused"),
        dict(type="health", kind="other"),
    ],
)
def test_run_rejects_values_outside_the_contract(ready, extra):
    with pytest.raises(psycopg.errors.CheckViolation):
        run(ready, "r1", **extra)


def test_search_window_must_not_be_inverted(ready):
    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            ready,
            "r1",
            since="2026-10-29T12:00:00Z",
            until="2026-10-29T06:00:00Z",
        )


def test_only_one_open_health_run_per_kind(ready):
    run(ready, "h1", type="health", kind="active", status="queued")
    run(ready, "h2", type="health", kind="recovery", status="running")
    with pytest.raises(psycopg.errors.UniqueViolation):
        run(ready, "h3", type="health", kind="active", status="running")


def test_a_new_health_run_may_start_after_the_previous_finished(ready):
    run(ready, "h1", type="health", kind="active", status="completed")
    run(ready, "h2", type="health", kind="active", status="running")
    assert one(ready, "SELECT count(*) FROM bm_runs WHERE kind='active'") == 2


def test_search_runs_are_not_limited_to_one_open_run(ready):
    run(ready, "s1")
    run(ready, "s2")
    assert one(ready, "SELECT count(*) FROM bm_runs WHERE type='search'") == 2


def test_clock_slot_is_unique(ready):
    run(ready, "s1", slot="2026-10-29T15:00:00Z")
    with pytest.raises(psycopg.errors.UniqueViolation), ready.transaction():
        run(ready, "s2", slot="2026-10-29T15:00:00Z")
    run(ready, "s3")
    run(ready, "s4")  # sem slot (manual): sem limite


def test_run_link_is_unique_per_run_and_link(ready):
    run(ready, "s1")
    link = link_id(ready)
    ready.execute("INSERT INTO bm_run_links(run_id,link_id) VALUES('s1',%s)", (link,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        ready.execute("INSERT INTO bm_run_links(run_id,link_id) VALUES('s1',%s)", (link,))


def test_a_link_has_at_most_one_open_search_read(ready):
    run(ready, "s1")
    run(ready, "s2")
    link = link_id(ready)
    ready.execute(
        "INSERT INTO bm_run_links(run_id,link_id,discovery_done) VALUES('s1',%s,false)", (link,)
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        ready.execute(
            "INSERT INTO bm_run_links(run_id,link_id,discovery_done) VALUES('s2',%s,false)",
            (link,),
        )


def test_closing_a_read_allows_the_next_one_and_health_rows_do_not_compete(ready):
    run(ready, "s1")
    run(ready, "s2")
    run(ready, "h1", type="health", kind="active")
    link = link_id(ready)
    ready.execute(
        "INSERT INTO bm_run_links(run_id,link_id,discovery_done) VALUES('s1',%s,false)", (link,)
    )
    ready.execute("UPDATE bm_run_links SET discovery_done=true WHERE run_id='s1'")
    ready.execute(
        "INSERT INTO bm_run_links(run_id,link_id,discovery_done) VALUES('s2',%s,false)", (link,)
    )
    ready.execute("INSERT INTO bm_run_links(run_id,link_id) VALUES('h1',%s)", (link,))


def test_run_link_rejects_unknown_states(ready):
    run(ready, "s1")
    with pytest.raises(psycopg.errors.CheckViolation):
        ready.execute(
            "INSERT INTO bm_run_links(run_id,link_id,state) VALUES('s1',%s,'weird')",
            (link_id(ready),),
        )


def test_discovery_task_identifies_one_document_slice(ready):
    run(ready, "s1")
    link = link_id(ready)
    ready.execute("INSERT INTO bm_run_links(run_id,link_id) VALUES('s1',%s)", (link,))
    insert = (
        "INSERT INTO bm_discovery_tasks(run_id,link_id,url,depth,slice) "
        "VALUES('s1',%s,'https://a.example/feed',0,%s)"
    )
    ready.execute(insert, (link, 0))
    ready.execute(insert, (link, 500))
    with pytest.raises(psycopg.errors.UniqueViolation), ready.transaction():
        ready.execute(insert, (link, 0))
    row = ready.execute("SELECT state,attempts,lane FROM bm_discovery_tasks LIMIT 1").fetchone()
    assert row == ("pending", 0, "primary")


def test_discovery_task_requires_its_run_link(ready):
    run(ready, "s1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        ready.execute(
            "INSERT INTO bm_discovery_tasks(run_id,link_id,url,depth) "
            "VALUES('s1',%s,'https://a.example/feed',0)",
            (link_id(ready),),
        )


def article(db: psycopg.Connection[Any], hash_: str = "h" * 64, **extra: Any) -> str:
    values: dict[str, Any] = dict(
        hash=hash_,
        url="https://a.example/n/1",
        host="a.example",
        site_id=int(one(db, "SELECT id FROM midias_ia_trusted_sources LIMIT 1")),
        first_run_id="s1",
        run_id="s1",
        **extra,
    )
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    db.execute(f"INSERT INTO bm_articles({names}) VALUES({marks})", tuple(values.values()))
    return hash_


def test_article_starts_pending_at_dedup(ready):
    run(ready, "s1")
    article(ready)
    row = ready.execute(
        "SELECT stage,state,attempts,seen_count,lane,payload,cost FROM bm_articles"
    ).fetchone()
    assert row == ("dedup", "pending", 0, 1, "primary", {}, {})


def test_article_hash_is_unique(ready):
    run(ready, "s1")
    article(ready)
    with pytest.raises(psycopg.errors.UniqueViolation):
        article(ready)


@pytest.mark.parametrize(
    "extra",
    [
        dict(stage="export"),
        dict(state="waiting"),
        dict(reason_class="oops"),
        dict(lane="turbo"),
        dict(outcome="weird"),
    ],
)
def test_article_rejects_values_outside_the_contract(ready, extra):
    run(ready, "s1")
    with pytest.raises(psycopg.errors.CheckViolation):
        article(ready, **extra)


def test_failed_articles_carry_the_failure_in_their_own_columns(ready):
    run(ready, "s1")
    article(
        ready,
        state="failed",
        stage="fetch",
        failed_stage="fetch",
        reason="http_403",
        reason_class="blocked",
        retryable=False,
        detail="HTTP 403 em https://a.example/n/1",
    )
    row = ready.execute(
        "SELECT failed_stage,reason,reason_class,retryable FROM bm_articles WHERE state='failed'"
    ).fetchone()
    assert row == ("fetch", "http_403", "blocked", False)


def test_article_queue_and_failure_screens_use_partial_indexes(ready):
    names = {
        r[0]
        for r in ready.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() "
            "AND tablename='bm_articles'"
        )
    }
    assert {"bm_articles_queue", "bm_articles_failed", "bm_articles_run"} <= names
    definition = one(
        ready,
        "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
        "AND indexname='bm_articles_queue'",
    )
    assert "pending" in definition and "retry" in definition


def test_host_state_and_document_cache_keys(ready):
    ready.execute("INSERT INTO bm_host_state(host) VALUES('a.example')")
    with pytest.raises(psycopg.errors.UniqueViolation), ready.transaction():
        ready.execute("INSERT INTO bm_host_state(host) VALUES('a.example')")
    site = int(one(ready, "SELECT id FROM midias_ia_trusted_sources LIMIT 1"))
    cache = (
        "INSERT INTO bm_document_cache(site_id,url_hash,revision,url,content_hash,document) "
        "VALUES(%s,'u1',%s,'https://a.example/feed','c1','\\x00')"
    )
    ready.execute(cache, (site, "r1"))
    ready.execute(cache, (site, "r2"))
    with pytest.raises(psycopg.errors.UniqueViolation), ready.transaction():
        ready.execute(cache, (site, "r1"))
