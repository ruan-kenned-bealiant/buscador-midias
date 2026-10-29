"""Estatísticas por rodada: números prontos em ``bm_runs.stats`` para o painel."""

from __future__ import annotations

import os
from typing import Any

import pytest
from report_support import key, seed

from buscador_midias.db import run_stats

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.fixture
def world(schema_name: str) -> dict[str, Any]:
    return seed(schema_name)


def stats_of(world: dict[str, Any], run_id: str = "r1") -> dict[str, Any]:
    with world["health"].connection() as db:
        return run_stats.refresh(db, run_id)


def test_the_funnel_counts_what_passed_each_stage(world):
    stats = stats_of(world)
    stages = stats["stages"]
    # 8 notícias passaram pela verificação no corpus
    assert sum(stages["dedup"].values()) == 8
    assert stages["dedup"]["succeeded"] == 8
    # fetch: 5 chegaram (persisted, late, waiting, broken e... ) — as que seguiram além dele
    assert stages["fetch"]["succeeded"] == 4  # persisted, nodate, waiting(context), broken
    assert stages["fetch"]["failed"] == 2 and stages["fetch"]["running"] == 1
    assert stages["context"]["failed"] == 1 and stages["context"]["pending"] == 1
    assert stages["context"]["succeeded"] == 1  # persisted
    assert stats["persisted"] == 1


def test_discovery_tasks_count_as_a_stage_of_their_own(world):
    discovery = stats_of(world)["stages"]["discovery"]
    assert discovery == {"succeeded": 1, "running": 1, "failed": 1}


def test_the_run_headline_numbers(world):
    stats = stats_of(world)
    assert (stats["links"], stats["links_done"]) == (3, 2)
    assert stats["occurrences"] == 30 and stats["unique_articles"] == 8
    assert stats["state"] == "running" and stats["final"] is False
    assert stats["adaptive_skipped"] == 2 and stats["head_filter"] == "enforce"
    assert stats["run_id"] == "r1" and stats["duration_s"] >= 0


def test_cost_is_summed_from_the_articles_of_the_run(world):
    stats = stats_of(world)
    assert stats["cost_usd"] == pytest.approx(0.03)
    assert stats["tokens_in"] == 150 and stats["tokens_out"] == 20
    assert stats["bright_requests"] == 2


def test_the_audit_sums_the_discovery_counters(world):
    audit = stats_of(world)["audit"]
    assert audit["head_cut"] == 3 and audit["not_modified"] == 1 and audit["discovery_reads"] == 3


def test_failures_are_grouped_by_stage_and_reason(world):
    failures = {(f["stage"], f["reason"]): f["n"] for f in stats_of(world)["failures"]}
    assert failures[("fetch", "http_403")] == 1 and failures[("fetch", "http_503")] == 1
    assert failures[("context", "Falha interna")] == 1
    assert failures[("discovery", "http_503")] == 1


def test_stats_are_stored_and_closed_runs_are_marked_final(world):
    stats = stats_of(world, "r0")
    assert stats["final"] is True and stats["stages"] == {} and stats["persisted"] == 0
    with world["health"].connection() as db:
        stored = db.execute("SELECT stats,stats_final FROM bm_runs WHERE id='r0'").fetchone()
    assert stored["stats_final"] and stored["stats"]["run_id"] == "r0"


def test_loading_returns_only_runs_that_have_stats(world):
    stats_of(world, "r0")
    with world["health"].connection() as db:
        loaded = run_stats.load(db, ["r0", "r1"])
    assert set(loaded) == {"r0"}


def test_finalize_pending_computes_closed_runs_without_final_stats(world):
    with world["health"].connection() as db:
        assert run_stats.finalize_pending(db) == ["r0"]
        assert run_stats.finalize_pending(db) == []


def test_an_unknown_run_is_a_404(world):
    from buscador_midias.catalog.store_errors import CatalogError

    with world["health"].connection() as db, pytest.raises(CatalogError) as error:
        run_stats.refresh(db, "nope")
    assert error.value.status == 404 and key("x")


def test_maintenance_closes_the_numbers_of_a_run_it_just_finished(world):
    from buscador_midias.db.store import WorkStore

    store = WorkStore(world["health"].schema)
    with store.connection() as db:
        db.execute("UPDATE bm_run_links SET discovery_done=true,state='completed'")
        db.execute("UPDATE bm_discovery_tasks SET state='done'")
        db.execute(
            "UPDATE bm_articles SET state='done',outcome='known' "
            "WHERE state IN ('pending','running','failed')"
        )
    store.maintain()
    with store.connection() as db:
        rows = {r["id"]: r for r in db.execute("SELECT id,status,stats_final FROM bm_runs")}
    assert rows["r1"]["status"] == "partial" and rows["r1"]["stats_final"] is True
