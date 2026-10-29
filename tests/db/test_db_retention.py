"""Limpeza periódica de ``bm_articles``, uma categoria por vez, e o acompanhamento de cada uma."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from report_support import seed

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db import cli
from buscador_midias.db.retention import CATEGORIES, RetentionRepository, next_run

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

DISCARDED = ("outside_window", "date_unknown", "no_text", "excluded_content", "no_entities")


@pytest.fixture
def repo(schema_name: str) -> RetentionRepository:
    world = seed(schema_name)
    with world["health"].connection() as db:
        db.execute("DELETE FROM bm_articles")  # parte de um banco sem notícias
    repository = RetentionRepository(schema_name)
    repository.site_id = world["sites"][0]["id"]  # type: ignore[attr-defined]
    return repository


def add(
    repo: RetentionRepository,
    name: str,
    *,
    state: str = "done",
    outcome: str | None = None,
    days: float = 0,
    seen_days: float | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Insere uma notícia ``days`` dias parada (e vista pela última vez há ``seen_days``)."""
    seen = days if seen_days is None else seen_days
    with repo.connection() as db:
        db.execute(
            "INSERT INTO bm_articles(hash,url,host,site_id,first_run_id,run_id,stage,state,outcome,"
            "failed_stage,reason,reason_class,last_seen_at,updated_at,payload) VALUES(%s,%s,"
            "'alfa.example',%s,'r1','r1','persist',%s,%s,%s,%s,%s,"
            "now()-make_interval(secs=>%s),now()-make_interval(secs=>%s),%s)",
            (
                name,
                f"https://alfa.example/{name}",
                repo.site_id,  # type: ignore[attr-defined]
                state,
                outcome,
                "fetch" if state == "failed" else None,
                "http_503" if state == "failed" else None,
                "transient" if state == "failed" else None,
                seen * 86400,
                days * 86400,
                Jsonb(payload or {}),
            ),
        )
    return name


def remaining(repo: RetentionRepository) -> set[str]:
    with repo.connection() as db:
        return {r["hash"] for r in db.execute("SELECT hash FROM bm_articles").fetchall()}


# --- concluídas (gravadas e já conhecidas) ------------------------------------------------------


def test_finished_news_already_in_the_corpus_go_after_three_days(repo):
    add(repo, "old_persisted", outcome="persisted", days=4)
    add(repo, "old_known", outcome="known", days=4)
    add(repo, "new_persisted", outcome="persisted", days=2)

    assert repo.run("concluidas") == 2

    assert remaining(repo) == {"new_persisted"}


def test_the_work_that_still_needs_a_decision_is_never_removed(repo):
    for name, state, outcome in (
        ("failed", "failed", None),
        ("pending", "pending", None),
        ("running", "running", None),
        ("retry", "retry", None),
        ("evidence", "done", "evidence_pending"),
    ):
        add(repo, name, state=state, outcome=outcome, days=400)

    assert repo.run("concluidas") == 0
    assert repo.run("descartadas") == 0

    assert len(remaining(repo)) == 5


def test_a_news_item_listed_again_recently_stays(repo):
    add(repo, "relisted", outcome="persisted", days=10, seen_days=1)

    assert repo.run("concluidas") == 0


def test_discards_are_not_touched_by_the_finished_category(repo):
    add(repo, "discard", outcome="no_entities", days=30)

    assert repo.run("concluidas") == 0


# --- descartadas ----------------------------------------------------------------------------------


def test_discards_are_kept_longer_than_finished_news(repo):
    for outcome in DISCARDED:
        add(repo, f"old_{outcome}", outcome=outcome, days=15)
        add(repo, f"recent_{outcome}", outcome=outcome, days=13)
    add(repo, "persisted", outcome="persisted", days=15)

    assert repo.run("descartadas") == len(DISCARDED)

    assert remaining(repo) == {f"recent_{o}" for o in DISCARDED} | {"persisted"}


# --- corpo das descartadas ------------------------------------------------------------------------


def test_the_text_of_old_discards_is_dropped_but_the_audit_stays(repo):
    add(repo, "old", outcome="no_entities", days=2, payload=dict(body="x" * 100, title="T", n=1))
    add(repo, "fresh", outcome="no_entities", days=0.1, payload=dict(body="texto", title="T"))
    add(repo, "failed", state="failed", days=5, payload=dict(body="para reprocessar"))

    assert repo.run("corpo-descartadas") == 1

    with repo.connection() as db:
        rows = {r["hash"]: r["payload"] for r in db.execute("SELECT hash,payload FROM bm_articles")}
    assert rows["old"] == dict(title="T", n=1)
    assert rows["fresh"]["body"] == "texto"
    assert rows["failed"]["body"] == "para reprocessar"
    assert repo.run("corpo-descartadas") == 0


# --- mecânica ------------------------------------------------------------------------------------


def test_large_cleanups_are_done_in_small_committed_batches(repo):
    for n in range(7):
        add(repo, f"p{n}", outcome="persisted", days=9)

    assert repo.run("concluidas", batch=2) == 7

    assert remaining(repo) == set()


def test_the_age_can_be_changed_but_never_to_nothing(repo):
    add(repo, "p", outcome="persisted", days=2)

    assert repo.run("concluidas", days=1) == 1
    with pytest.raises(CatalogError):
        repo.run("concluidas", days=0)
    with pytest.raises(CatalogError):
        repo.run("tudo")
    with pytest.raises(CatalogError):
        repo.run("concluidas", batch=0)


def test_every_category_has_a_description_and_a_default_age():
    assert set(CATEGORIES) == {"concluidas", "descartadas", "corpo-descartadas"}
    assert all(c.description and c.days >= 1 for c in CATEGORIES.values())


def test_the_command_cleans_one_category_and_reports_it(repo, schema_name, capsys):
    add(repo, "old", outcome="persisted", days=5)

    assert cli.main(["clean", "concluidas", "--schema", schema_name]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report == dict(schema=schema_name, category="concluidas", days=3, removed=1)
    assert cli.main(["clean", "inexistente", "--schema", schema_name]) == 2


# --- acompanhamento ------------------------------------------------------------------------------


def history(repo: RetentionRepository) -> list[dict[str, Any]]:
    with repo.connection() as db:
        return db.execute("SELECT * FROM bm_cleanup_runs ORDER BY id").fetchall()


def test_every_run_is_recorded_with_what_it_removed(repo):
    add(repo, "old", outcome="persisted", days=5)

    repo.run("concluidas")
    repo.run("concluidas")

    first, second = history(repo)
    assert (first["category"], first["days"], first["removed"], first["error"]) == (
        "concluidas",
        3,
        1,
        None,
    )
    assert first["finished_at"] >= first["started_at"]
    assert second["removed"] == 0


def test_a_failed_run_is_recorded_with_its_error_and_still_raises(repo, monkeypatch):
    broken = replace(CATEGORIES["concluidas"], where="state='done' AND nonexistent_column=1")
    monkeypatch.setitem(CATEGORIES, "concluidas", broken)

    with pytest.raises(Exception, match="nonexistent_column"):
        repo.run("concluidas")

    [run] = history(repo)
    assert run["finished_at"] is not None and "UndefinedColumn" in run["error"]


def test_the_status_shows_schedule_last_run_and_what_is_waiting(repo):
    add(repo, "old", outcome="persisted", days=5)
    add(repo, "young", outcome="persisted", days=1)
    add(repo, "discard", outcome="no_entities", days=20, payload=dict(body="x"))
    repo.run("corpo-descartadas")

    status = {row["category"]: row for row in repo.status()}

    assert list(status) == list(CATEGORIES)
    assert status["concluidas"]["eligible"] == 1 and status["concluidas"]["last"] is None
    assert status["descartadas"]["eligible"] == 1
    assert status["corpo-descartadas"]["eligible"] == 0
    assert status["corpo-descartadas"]["last"]["removed"] == 1
    assert status["corpo-descartadas"]["last"]["state"] == "done"
    assert status["concluidas"]["at"] == CATEGORIES["concluidas"].at
    assert status["concluidas"]["next_run"] > datetime.now(UTC)


def test_a_run_that_never_finished_is_shown_as_interrupted_when_old(repo):
    with repo.connection() as db:
        db.execute(
            "INSERT INTO bm_cleanup_runs(category,days,started_at) VALUES "
            "('concluidas',3,now()-interval '3 hours'),('descartadas',14,now()-interval '1 minute')"
        )

    status = {row["category"]: row["last"]["state"] for row in repo.status() if row["last"]}

    assert status == dict(concluidas="interrupted", descartadas="running")


def test_the_next_run_is_the_next_clock_time_in_the_configured_zone():
    before = datetime(2026, 10, 30, 5, 0, tzinfo=UTC)  # 02:00 em Brasília
    after = datetime(2026, 10, 30, 7, 0, tzinfo=UTC)  # 04:00 em Brasília

    assert next_run("03:30", before) == datetime(2026, 10, 30, 6, 30, tzinfo=UTC)
    assert next_run("03:30", after) == datetime(2026, 10, 31, 6, 30, tzinfo=UTC)


def test_old_history_is_trimmed_so_the_log_does_not_grow_forever(repo):
    with repo.connection() as db:
        db.execute(
            "INSERT INTO bm_cleanup_runs(category,days,started_at,finished_at) "
            "VALUES('concluidas',3,now()-interval '120 days',now()-interval '120 days')"
        )

    repo.run("concluidas")

    assert len(history(repo)) == 1
