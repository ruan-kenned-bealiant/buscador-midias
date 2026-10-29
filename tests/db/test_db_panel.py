"""O JSON da tela Painel: rodadas, falhas, auditoria, saúde, corpus e o estado de agora."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest
from report_support import seed

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.reports import ReportRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

CORPUS = (
    "CREATE TABLE midias_ia_articles (id BIGSERIAL PRIMARY KEY, origin TEXT, "
    "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
)
QUEUES = [
    dict(stage="dedup", lane="primary", queue="bm.dedup", messages=4, consumers=1),
    dict(stage="fetch", lane="primary", queue="bm.fetch", messages=0, consumers=2),
    dict(stage="fetch", lane="recovery", queue="bm.fetch.recovery", messages=None, consumers=None),
]


@pytest.fixture
def world(schema_name: str) -> dict[str, Any]:
    data = seed(schema_name)
    reports = ReportRepository(schema_name, corpus_schema=schema_name)
    with reports.connection() as db:
        db.execute(CORPUS)
        db.execute("INSERT INTO midias_ia_articles(origin) VALUES('crawler'),('midias_ia')")
    return dict(reports=reports, **data)


def panel(world: dict[str, Any], days: int = 7, **extra: Any) -> dict[str, Any]:
    return world["reports"].panel(days, broker=lambda: QUEUES, **extra)


def test_the_panel_has_every_block_the_screen_reads(world):
    result = panel(world)
    assert {
        "generated_at",
        "days",
        "last",
        "previous",
        "runs",
        "pending_stats",
        "failures",
        "audit",
        "health",
        "corpus",
        "now",
    } <= set(result)
    assert result["days"] == 7 and result["pending_stats"] == 0


def test_runs_come_oldest_first_with_the_last_two_exposed(world):
    result = panel(world)
    assert [r["run_id"] for r in result["runs"]] == ["r0", "r1"]
    assert result["last"]["run_id"] == "r1" and result["previous"]["run_id"] == "r0"
    assert result["last"]["persisted"] == 1 and result["last"]["links_done"] == 2


def test_missing_stats_are_computed_and_stored_on_demand(world):
    panel(world)
    with world["reports"].connection() as db:
        stored = db.execute("SELECT id,stats_final FROM bm_runs ORDER BY id").fetchall()
    assert [(r["id"], r["stats_final"]) for r in stored] == [("r0", True), ("r1", False)]


def test_runs_beyond_the_on_demand_budget_are_counted_as_pending(world, monkeypatch):
    monkeypatch.setattr("buscador_midias.db.panel.MAX_ON_DEMAND", 1)
    result = panel(world)
    assert len(result["runs"]) == 1 and result["pending_stats"] == 1


def test_an_open_run_is_recomputed_when_its_numbers_are_stale(world):
    panel(world)
    with world["reports"].connection() as db:
        db.execute("UPDATE bm_articles SET state='done',outcome='known' WHERE state='running'")
        db.execute(
            "UPDATE bm_runs SET stats=jsonb_set(stats,'{computed_at}',"
            "to_jsonb(to_char((now()-interval '5 minutes') AT TIME ZONE 'UTC',"
            "'YYYY-MM-DD\"T\"HH24:MI:SS+00:00'))) "
            "WHERE id='r1'"
        )
    assert panel(world)["last"]["stages"]["fetch"].get("running", 0) == 0


def test_failures_merge_the_runs_and_carry_their_share(world):
    failures = panel(world)["failures"]
    assert {(f["stage"], f["reason"]) for f in failures} >= {
        ("fetch", "http_403"),
        ("context", "Falha interna"),
    }
    assert sum(f["pct"] for f in failures) == pytest.approx(100, abs=0.5)


def test_the_audit_only_counts_runs_with_the_cut_enforced(world):
    audit = panel(world)["audit"]
    assert audit["discovery_reads"] == 3 and audit["cold_skipped"] == 2
    with world["reports"].connection() as db:
        db.execute("UPDATE bm_runs SET config=jsonb_set(config,'{head_filter}','\"shadow\"')")
        db.execute("UPDATE bm_runs SET stats='{}',stats_final=false")
    assert panel(world)["audit"]["discovery_reads"] == 0


def test_health_lists_a_day_by_day_series_and_the_last_run_of_each_routine(world):
    with world["reports"].connection() as db:
        db.execute(
            "INSERT INTO bm_events(entity,entity_id,action,actor) VALUES "
            "('endpoint',1,'health_inactivate','health'),('endpoint',2,'health_inactivate',"
            "'health'),('endpoint',3,'health_reactivate','health'),('endpoint',1,'update','user')"
        )
        db.execute(
            "INSERT INTO bm_runs(id,type,kind,trigger,status,config,total,finished_at) "
            "VALUES('h1','health','active','scheduled','completed','{}',5,now())"
        )
        db.execute(
            "INSERT INTO bm_run_links(run_id,link_id,site_id,host,state,category) VALUES "
            "('h1',1,1,'a','completed','healthy'),('h1',2,1,'a','completed','stale')"
        )
    health = panel(world, 3)["health"]
    today = health["daily"][-1]
    assert len(health["daily"]) == 3 and today["inactivated"] == 2 and today["reactivated"] == 1
    assert health["last"]["active"]["counts"] == {"healthy": 1, "stale": 1}
    assert health["last"]["active"]["total"] == 5 and "recovery" not in health["last"]


def test_the_corpus_block_rebuilds_the_total_per_day(world):
    corpus = panel(world)["corpus"]
    assert corpus["total"] == 2 and corpus["daily"][-1]["added"] == 2
    assert corpus["daily"][-1]["crawler"] == 1 and corpus["daily"][-1]["total"] == 2


def test_the_corpus_block_is_absent_when_the_table_does_not_exist(world):
    other = ReportRepository(world["reports"].schema, corpus_schema="nonexistent_schema")
    assert other.panel(7, broker=lambda: QUEUES)["corpus"] is None


def test_now_describes_the_open_run_queues_and_workers(world):
    now = panel(world)["now"]
    assert now["open_run"] == "r1"
    queues = {(q["stage"], q["state"]): q["n"] for q in now["queues"]}
    assert queues[("fetch", "running")] == 1 and queues[("context", "pending")] == 1
    assert now["workers_online"] == 3 and now["broker"]["ok"] is True
    assert now["broker"]["queues"][0]["messages"] == 4
    assert now["disk_free_gb"] > 0
    assert now["catalog"]["active_links"] == 3 and now["catalog"]["active_sites"] == 2
    assert set(now["settings"]) >= {"head_filter", "schedule_hours", "timezone"}


def test_a_broker_that_fails_does_not_break_the_panel(world):
    def down() -> list[dict[str, Any]]:
        raise OSError("sem broker")

    now = world["reports"].panel(7, broker=down)["now"]
    assert now["broker"] == dict(ok=False, queues=[]) and now["workers_online"] == 0


def test_the_next_executions_come_from_the_settings(world):
    with world["reports"].connection() as db:
        db.execute(
            "UPDATE bm_settings SET value=jsonb_set(value,'{enabled}','true') WHERE scope='search'"
        )
    now = panel(world)["now"]
    assert now["next_round"] and datetime.fromisoformat(now["next_round"]) > datetime.now(UTC)
    assert set(now["next_health"]) == {"active", "recovery"}


@pytest.mark.parametrize("days", [0, 31, "7", None])
def test_the_window_is_one_to_thirty_days(world, days):
    with pytest.raises(CatalogError):
        panel(world, days)
