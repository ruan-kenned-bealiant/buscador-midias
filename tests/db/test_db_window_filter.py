"""Pré-filtro de janela: fecha, sem HTTP, o que já está provadamente fora do dia."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from buscador_midias.db.store import WorkStore
from buscador_midias.ingestion.flow import publication_outside

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

SINCE = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
UNTIL = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
OLD = "2026-10-01T10:00:00+00:00"
TODAY = "2026-10-29T10:00:00+00:00"


@pytest.fixture
def store(schema_name: str) -> WorkStore:
    work = WorkStore(schema_name)
    work.install()
    with work.connection() as db:
        db.execute(
            "INSERT INTO midias_ia_trusted_sources(identity,name,url) VALUES('a.example','A','https://a.example')"
        )
        db.execute(
            "INSERT INTO bm_links(site_id,url,kind,mechanism) "
            "VALUES(1,'https://a.example/feed','feed','rss')"
        )
        db.execute(
            "INSERT INTO bm_runs(id,type,trigger,status,config,since,until) "
            "VALUES('r1','search','scheduled','running',%s,%s,%s)",
            (
                Jsonb(
                    dict(max_attempts=3, mechanisms=dict(rss=dict(version=1, parameters=dict(a=1))))
                ),
                SINCE,
                UNTIL,
            ),
        )
    return work


def article(store: WorkStore, name: str, url: str | None = None, **columns: Any) -> str:
    values: dict[str, Any] = dict(
        hash=name.ljust(64, "0"),
        url=url or f"https://a.example/{name}",
        host="a.example",
        site_id=1,
        link_id=1,
        first_run_id="r1",
        run_id="r1",
    )
    values.update(columns)
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    with store.connection() as db:
        db.execute(f"INSERT INTO bm_articles({names}) VALUES({marks})", tuple(values.values()))
    return str(values["hash"])


def hinted(published: str) -> Jsonb:
    return Jsonb(dict(published_hint=published, published_hint_basis="xml_publication"))


def test_only_waiting_articles_with_a_date_hint_are_candidates(store):
    dated_url = article(store, "dated", "https://a.example/2026/10/01/noticia")
    hint = article(store, "hint", payload=hinted(OLD))
    article(store, "plain")
    article(store, "ctx", "https://a.example/2026/10/01/x", stage="context")
    article(
        store,
        "busy",
        "https://a.example/2026/10/01/y",
        state="running",
        owner="w",
        locked_until="2999-01-01Z",
    )
    article(store, "later", "https://a.example/2026/10/01/z", next_attempt_at="2999-01-01Z")
    found = {p["id"] for p in store.window_candidates()}
    assert found == {dated_url, hint}


def test_a_candidate_has_the_shape_the_engine_rules_expect(store):
    key = article(store, "hint", payload=hinted(OLD))
    [preview] = store.window_candidates()
    assert preview["id"] == key and preview["stage"] == "dedup"
    assert preview["article"]["url"] == "https://a.example/hint"
    assert preview["article"]["payload"]["published_hint"] == OLD
    assert preview["run"]["since"] == SINCE and preview["run"]["until"] == UNTIL
    assert preview["snapshot"]["source"]["id"] == 1
    assert preview["snapshot"]["parameters"] == dict(a=1)
    assert publication_outside(preview)["status"] == "outside_window"


def test_the_cursor_pages_through_candidates_in_order(store):
    keys = sorted(article(store, f"h{n}", payload=hinted(OLD)) for n in range(5))
    first = store.window_candidates()
    assert [p["id"] for p in first] == keys
    assert [p["id"] for p in store.window_candidates(keys[2])] == keys[3:]
    assert store.window_candidates(keys[-1]) == []


def test_decisions_close_the_articles_without_any_request(store):
    old = article(store, "old", payload=hinted(OLD))
    fresh = article(store, "fresh", payload=hinted(TODAY))
    decisions = []
    for preview in store.window_candidates():
        outside = publication_outside(preview)
        if outside:
            decisions.append((preview["stage"], preview["id"], outside))
    assert store.apply_decisions(decisions, "w:window") == 1
    with store.connection() as db:
        closed = db.execute("SELECT * FROM bm_articles WHERE hash=%s", (old,)).fetchone()
        kept = db.execute("SELECT state FROM bm_articles WHERE hash=%s", (fresh,)).fetchone()
    assert (closed["state"], closed["outcome"]) == ("done", "outside_window")
    assert closed["payload"]["date_basis"] == "xml_publication"
    assert closed["cost"]["dedup"]["avoided_article_requests"] == 1
    assert kept["state"] == "pending"


def test_a_decision_for_an_article_that_moved_on_is_skipped(store):
    key = article(store, "old", payload=hinted(OLD))
    [preview] = store.window_candidates()
    decision = [(preview["stage"], key, publication_outside(preview))]
    with store.connection() as db:
        db.execute("UPDATE bm_articles SET state='running',owner='x',locked_until='2999-01-01Z'")
    assert store.apply_decisions(decision, "w:window") == 0


def test_decisions_group_by_stage(store):
    a = article(store, "a", payload=hinted(OLD))
    b = article(store, "b", payload=hinted(OLD), stage="fetch")
    decisions = [(p["stage"], p["id"], publication_outside(p)) for p in store.window_candidates()]
    assert {d[0] for d in decisions} == {"dedup", "fetch"}
    assert store.apply_decisions(decisions, "w:window") == 2
    with store.connection() as db:
        states = db.execute("SELECT hash,state FROM bm_articles").fetchall()
    assert {r["hash"]: r["state"] for r in states} == {a: "done", b: "done"}
