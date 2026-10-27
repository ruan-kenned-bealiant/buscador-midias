# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Retroativo do ``lastmod``: só encerra o que é comprovadamente anterior à janela."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest, next_job  # noqa: F401

from buscador_midias.ingestion.lastmod_backfill import backfill

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

OLD, NEW, UNKNOWN = (f"https://jose.example/{name}" for name in ("velha", "nova", "sem-data"))


def seed(ingest, **flags):
    """Rodada com três URLs em dedup; ``flags`` marca candidatos como na descoberta em shadow."""
    run = ingest.create_run({})
    ingest.finish(
        next_job(ingest, "discovery"),
        dict(candidates=[dict(url=u, **flags.get(u, {})) for u in (OLD, NEW, UNKNOWN)]),
    )
    return run["id"], run["since"]


def stale(since):
    return (since - timedelta(days=9)).isoformat()


def states(ingest):
    with ingest.connection() as db:
        return {
            r["url"]: (r["state"], r["outcome"])
            for r in db.execute(
                "SELECT a.url,j.state,j.outcome FROM ingest_jobs j JOIN ingest_articles a "
                "ON a.hash=j.hash WHERE j.stage='dedup'"
            ).fetchall()
        }


def test_report_is_a_dry_run_that_changes_nothing(ingest):
    run_id, since = seed(ingest)
    report = backfill(ingest, run_id, {OLD: stale(since)})
    assert report["open_tasks"] == 3 and report["excludable"] == 1 and report["applied"] == 0
    assert report["by_stage"] == {"dedup": 1} and report["violations"] == 0
    assert set(states(ingest).values()) == {("pending", None)}


def test_apply_finishes_only_the_provably_old_task_without_a_request(ingest):
    run_id, since = seed(ingest)
    report = backfill(ingest, run_id, {OLD: stale(since)}, apply=True)
    assert report["applied"] == 1
    found = states(ingest)
    assert found[OLD] == ("succeeded", "fora_da_janela_por_lastmod")
    assert found[NEW][0] == found[UNKNOWN][0] == "pending"
    with ingest.connection() as db:
        article = db.execute(
            "SELECT status,payload FROM ingest_articles WHERE url=%s", (OLD,)
        ).fetchone()
        fetches = db.execute("SELECT count(*) n FROM ingest_jobs WHERE stage='fetch'").fetchone()
    assert article["status"] == "outside_window"
    assert article["payload"]["date_basis"] == "lastmod_upper_bound"
    assert article["payload"]["exclusion"]["rule"] == "lastmod_before_window"
    assert fetches["n"] == 0


def test_apply_is_idempotent(ingest):
    run_id, since = seed(ingest)
    hints = {OLD: stale(since)}
    assert backfill(ingest, run_id, hints, apply=True)["applied"] == 1
    again = backfill(ingest, run_id, hints, apply=True)
    assert again["open_tasks"] == 2 and again["excludable"] == 0 and again["applied"] == 0


def test_urls_flagged_in_shadow_are_excluded_without_any_cached_hint(ingest):
    flag = dict(shadow_lastmod_exclusion=dict(rule="lastmod_before_window", lastmod="2026-01-01"))
    run_id, _ = seed(ingest, **{OLD: flag})
    report = backfill(ingest, run_id, {}, apply=True)
    assert report["applied"] == 1 and states(ingest)[OLD][1] == "fora_da_janela_por_lastmod"


def test_a_recent_or_undated_hint_never_excludes(ingest):
    run_id, since = seed(ingest)
    hints = {NEW: (since + timedelta(hours=1)).isoformat(), UNKNOWN: "não é data"}
    assert backfill(ingest, run_id, hints, apply=True)["applied"] == 0


def test_articles_already_in_the_corpus_are_left_for_evidence_repair(ingest):
    run_id, since = seed(ingest)
    with ingest.connection() as db:
        db.execute(
            'UPDATE ingest_articles SET payload=payload||\'{"existing_hash":"abc"}\' WHERE url=%s',
            (OLD,),
        )
    report = backfill(ingest, run_id, {OLD: stale(since)}, apply=True)
    assert report["open_tasks"] == 2 and report["applied"] == 0


def test_a_confirmed_in_window_article_that_the_rule_would_exclude_blocks_the_apply(ingest):
    run_id, since = seed(ingest)
    job = next_job(ingest, "dedup")
    ingest.finish(job, dict(next="fetch"))
    fetch = next_job(ingest, "fetch")
    ingest.finish(fetch, dict(outcome="texto_confirmado"))
    with ingest.connection() as db:
        url = db.execute(
            "SELECT a.url FROM ingest_jobs j JOIN ingest_articles a ON a.hash=j.hash WHERE j.id=%s",
            (fetch["id"],),
        ).fetchone()["url"]
    other = OLD if url != OLD else NEW
    report = backfill(ingest, run_id, {url: stale(since), other: stale(since)}, apply=True)
    assert report["violations"] == 1 and report["violation_samples"][0]["url"] == url
    assert report["applied"] == 0
    assert states(ingest)[other][0] == "pending"
