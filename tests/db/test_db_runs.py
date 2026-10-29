"""Criar a rodada de busca: links selecionados, slot de relógio, rotas diárias e links frios."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.runs import cold_link_ids, create_search_run
from buscador_midias.db.schema import install
from buscador_midias.db.settings import SEARCH_DEFAULTS

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

NOON = datetime(2026, 10, 29, 15, 5, tzinfo=UTC)  # 12:05 em Brasília (slot das 12h)
FIRST = datetime(2026, 10, 29, 4, 5, tzinfo=UTC)  # 01:05 em Brasília (slot das 01h)
MIDDAY = datetime(2026, 10, 29, 9, 5, tzinfo=UTC)  # 06:05 em Brasília (slot das 06h)
CONFIG = {**SEARCH_DEFAULTS, "enabled": True}


@pytest.fixture
def q(db: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    install(db)
    for model in ("rss", "sitemap", "auto"):
        db.execute(
            "INSERT INTO bm_mechanisms(model,parameters,version) VALUES(%s,%s,3)",
            (model, Jsonb(dict(max_requests=48))),
        )
    db.execute(
        "INSERT INTO midias_ia_trusted_sources(identity,name,url) VALUES('a.example','A','https://a.example')"
    )
    db.execute(
        "INSERT INTO midias_ia_trusted_sources(identity,name,url) VALUES('b.example','B','https://b.example')"
    )
    db.row_factory = dict_row  # type: ignore[assignment]
    return db


def link(db: psycopg.Connection[Any], path: str, *, site: int = 1, **columns: Any) -> int:
    values: dict[str, Any] = dict(
        site_id=site,
        url=f"https://a.example/{path}",
        kind="feed",
        mechanism="rss",
        read_order="newest_first",
        access_transport="curl_chrome_h2",
    )
    values.update(columns)
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    row = db.execute(
        f"INSERT INTO bm_links({names}) VALUES({marks}) RETURNING id", tuple(values.values())
    ).fetchone()
    assert row
    return int(row["id"])


def ids(db: psycopg.Connection[Any], run_id: str) -> set[int]:
    rows = db.execute("SELECT link_id FROM bm_run_links WHERE run_id=%s", (run_id,)).fetchall()
    return {r["link_id"] for r in rows}


def scheduled(db: psycopg.Connection[Any], now: datetime = NOON, **config: Any) -> Any:
    return create_search_run(db, {}, config={**CONFIG, **config}, scheduled=True, now=now)


def test_a_scheduled_run_freezes_window_slot_config_and_mechanisms(q):
    one = link(q, "feed")
    run = scheduled(q)
    assert run["links"] == 1 and run["type"] == "search" and run["trigger"] == "scheduled"
    assert run["status"] == "running" and run["total"] == 1
    assert run["slot"] == datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
    assert run["since"] == datetime(2026, 10, 29, 3, 0, tzinfo=UTC) and run["until"] == NOON
    assert run["config"]["workers"]["fetch"] == 2
    assert run["config"]["mechanisms"]["rss"] == dict(version=3, parameters=dict(max_requests=48))
    assert ids(q, run["id"]) == {one}


def test_every_selected_link_gets_a_frozen_read_and_a_root_task(q):
    one = link(q, "feed", site=2, url="https://b.example/rss")
    run = scheduled(q)
    read = q.execute("SELECT * FROM bm_run_links WHERE run_id=%s", (run["id"],)).fetchone()
    assert read["link_id"] == one and read["site_id"] == 2 and read["host"] == "b.example"
    assert (read["link_version"], read["site_version"], read["mechanism_version"]) == (1, 1, 3)
    assert read["state"] == "pending" and read["discovery_done"] is False
    task = q.execute("SELECT * FROM bm_discovery_tasks WHERE run_id=%s", (run["id"],)).fetchone()
    assert (task["url"], task["depth"], task["slice"], task["state"], task["host"]) == (
        "https://b.example/rss",
        0,
        0,
        "pending",
        "b.example",
    )
    state = q.execute("SELECT last_read_at FROM bm_link_state WHERE link_id=%s", (one,)).fetchone()
    assert state and state["last_read_at"] is not None


def test_only_active_links_of_active_sites_are_read(q):
    ok = link(q, "ok")
    link(q, "off", active=0)
    link(q, "deleted", deleted_at="2026-10-01Z", active=0)
    link(q, "other", site=2, url="https://b.example/x")
    q.execute("UPDATE midias_ia_trusted_sources SET active=false WHERE id=2")
    assert ids(q, scheduled(q)["id"]) == {ok}


def test_the_same_clock_slot_creates_only_one_run(q):
    link(q, "feed")
    assert scheduled(q) is not None
    q.execute("UPDATE bm_run_links SET discovery_done=true")
    assert scheduled(q) is None
    assert scheduled(q, MIDDAY) is not None


def test_scheduled_runs_wait_for_the_slot_and_for_being_enabled(q):
    link(q, "feed")
    assert scheduled(q, NOON + timedelta(minutes=20)) is None  # fora da tolerância de 15 min
    assert scheduled(q, enabled=False) is None
    assert q.execute("SELECT count(*) AS n FROM bm_runs").fetchone()["n"] == 0


def test_a_link_with_an_open_read_is_never_read_twice(q):
    busy = link(q, "busy")
    free = link(q, "free")
    first = scheduled(q, MIDDAY)
    q.execute(
        "UPDATE bm_run_links SET discovery_done=true WHERE run_id=%s AND link_id=%s",
        (first["id"], free),
    )
    second = scheduled(q, NOON)
    assert ids(q, second["id"]) == {free}
    assert busy not in ids(q, second["id"])


def test_nothing_to_read_means_no_run_for_the_schedule_and_a_conflict_for_a_person(q):
    assert scheduled(q) is None
    with pytest.raises(CatalogError) as error:
        create_search_run(q, {}, config=CONFIG, scheduled=False, now=NOON)
    assert error.value.status == 409


def test_a_manual_run_can_choose_links_and_never_takes_a_clock_slot(q):
    one, two = link(q, "one"), link(q, "two")
    run = create_search_run(q, {"link_ids": [two]}, config=CONFIG, scheduled=False, now=NOON)
    assert ids(q, run["id"]) == {two} and run["slot"] is None and run["trigger"] == "manual"
    assert one not in ids(q, run["id"])
    again = create_search_run(q, {"link_ids": [one]}, config=CONFIG, scheduled=False, now=NOON)
    assert ids(q, again["id"]) == {one}


@pytest.mark.parametrize(
    "data",
    [{"surprise": 1}, {"link_ids": "x"}, {"link_ids": [0]}, {"link_ids": [True]}],
)
def test_invalid_options_are_rejected(q, data):
    link(q, "feed")
    with pytest.raises(CatalogError):
        create_search_run(q, data, config=CONFIG, scheduled=False, now=NOON)


def test_daily_routes_keep_archives_and_reviews_out_and_feeds_in(q):
    feed = link(q, "feed")
    archive = link(q, "archive.xml", kind="sitemap", mechanism="sitemap", route="archive")
    review = link(q, "review", route="review")
    updates = link(q, "updates.xml", kind="sitemap", mechanism="sitemap", route="updates")
    plain = link(q, "plain.xml", kind="sitemap", mechanism="sitemap")
    auto_feed = link(q, "auto", mechanism="auto")
    chosen = ids(q, scheduled(q)["id"])
    assert chosen == {feed, updates, auto_feed}
    assert not chosen & {archive, review, plain}


def test_every_active_link_is_read_when_daily_routes_are_not_required(q):
    plain = link(q, "plain.xml", kind="sitemap", mechanism="sitemap")
    archive = link(q, "archive.xml", kind="sitemap", mechanism="sitemap", route="archive")
    chosen = ids(q, scheduled(q, daily_routes_only=False)["id"])
    assert {plain, archive} <= chosen


# --- leitura adaptativa ---------------------------------------------------------------------


def history(q, link_id: int, reads: int, *, new: int = 0, state: str = "completed", days: int = 1):
    """``reads`` leituras encerradas do link, da mais velha para a mais nova."""
    for n in range(reads):
        ident = f"h{link_id}-{n}-{days}"
        q.execute(
            "INSERT INTO bm_runs(id,type,trigger,status,config,created_at) "
            "VALUES(%s,'search','scheduled','completed','{}',now()-%s*interval '1 hour')",
            (ident, (reads - n) + days * 24 - 24),
        )
        q.execute(
            "INSERT INTO bm_run_links(run_id,link_id,state,discovery_done,new_articles) "
            "VALUES(%s,%s,%s,true,%s)",
            (ident, link_id, state, new),
        )


def test_a_link_read_often_without_a_single_new_article_is_cold(q):
    cold = link(q, "cold")
    history(q, cold, 4)
    assert cold_link_ids(q) == {cold}


def test_a_link_that_found_something_a_failed_read_or_little_history_is_not_cold(q):
    found, broken, young = link(q, "found"), link(q, "broken"), link(q, "young")
    history(q, found, 3)
    history(q, found, 1, new=2, days=2)
    history(q, broken, 3)
    history(q, broken, 1, state="error", days=2)
    history(q, young, 3)
    assert cold_link_ids(q) == set()


def test_only_the_latest_reads_count_and_old_ones_expire(q):
    revived, stale = link(q, "revived"), link(q, "stale")
    history(q, revived, 1, new=3, days=3)  # a mais antiga achou algo, as 4 mais novas não
    history(q, revived, 4, days=1)
    history(q, stale, 4, days=9)  # leituras fora da janela de 7 dias
    assert cold_link_ids(q) == {revived}


def test_the_middle_rounds_skip_cold_links_and_the_boundary_rounds_read_them(q):
    cold, warm = link(q, "cold"), link(q, "warm")
    history(q, cold, 4)
    enforce = dict(adaptive_polling="enforce")
    middle = scheduled(q, MIDDAY, **enforce)
    assert ids(q, middle["id"]) == {warm}
    assert middle["config"]["adaptive_skipped"] == 1
    q.execute("UPDATE bm_run_links SET discovery_done=true")
    assert ids(q, scheduled(q, FIRST, **enforce)["id"]) == {cold, warm}
    q.execute("UPDATE bm_run_links SET discovery_done=true")
    last = datetime(2026, 10, 29, 19, 5, tzinfo=UTC)  # 16:05
    assert ids(q, scheduled(q, last, **enforce)["id"]) == {cold, warm}


def test_cold_links_are_always_read_when_the_feature_is_off_or_the_run_is_manual(q):
    cold = link(q, "cold")
    link(q, "warm")
    history(q, cold, 4)
    assert cold in ids(q, scheduled(q, MIDDAY, adaptive_polling="off")["id"])
    q.execute("UPDATE bm_run_links SET discovery_done=true")
    manual = create_search_run(
        q, {}, config={**CONFIG, "adaptive_polling": "enforce"}, scheduled=False, now=MIDDAY
    )
    assert cold in ids(q, manual["id"])
