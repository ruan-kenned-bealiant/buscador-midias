# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Dados do painel: última rodada, histórico, agora, falhas, saúde e auditoria."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest  # noqa: F401

from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion import panel
from buscador_midias.ingestion.api import dispatch
from buscador_midias.ingestion.flow import next_slot
from buscador_midias.ingestion.panel import build_panel
from buscador_midias.ingestion.run_stats import refresh_run_stats

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)
ZONE = "America/Sao_Paulo"


def make_run(
    ingest, hours_ago, *, finished=True, persisted=3, failed=0, reason="HTTP 403", head="enforce"
):
    """Rodada sintética: tarefas de cada etapa, ``persisted`` gravadas e ``failed`` falhas."""
    at = datetime.now(UTC) - timedelta(hours=hours_ago)
    run, poll = uuid.uuid4().hex, uuid.uuid4().hex
    with ingest.connection() as db:
        db.execute(
            "INSERT INTO ingest_runs(id,trigger,state,since,until,config,created_at,finished_at) "
            "VALUES(%s,'scheduled',%s,%s,%s,%s,%s,%s)",
            (
                run,
                "partial" if finished else "running",
                at - timedelta(hours=1),
                at,
                Jsonb({"adaptive_skipped": 2, "head_filter": head}),
                at,
                at + timedelta(minutes=30) if finished else None,
            ),
        )
        db.execute(
            "INSERT INTO ingest_polls(id,run_id,link_id,snapshot,discovery_done,created_at) "
            "VALUES(%s,%s,1,%s,%s,%s)",
            (poll, run, Jsonb({}), finished, at),
        )
        for stage, ok in (
            ("discovery", 1),
            ("fetch", persisted),
            ("context", persisted),
            ("persist", persisted),
        ):
            for n in range(ok):
                db.execute(
                    "INSERT INTO ingest_jobs(id,stage,subject,run_id,poll_id,state) "
                    "VALUES(%s,%s,%s,%s,%s,'succeeded')",
                    (uuid.uuid4().hex, stage, f"{stage}-{run}-{n}", run, poll),
                )
        for n in range(failed):
            db.execute(
                "INSERT INTO ingest_jobs(id,stage,subject,run_id,poll_id,state,outcome) "
                "VALUES(%s,'fetch',%s,%s,%s,'failed',%s)",
                (uuid.uuid4().hex, f"bad-{run}-{n}", run, poll, reason),
            )
        if not finished:
            db.execute(
                "INSERT INTO ingest_jobs(id,stage,subject,run_id,poll_id,state) "
                "VALUES(%s,'fetch',%s,%s,%s,'queued')",
                (uuid.uuid4().hex, f"q-{run}", run, poll),
            )
    return run


def test_an_empty_database_still_returns_the_whole_shape(ingest):
    result = build_panel(ingest, days=7)

    assert result["last"] is None and result["previous"] is None and result["runs"] == []
    assert result["failures"] == [] and result["days"] == 7
    assert result["now"]["queues"] == [] and result["now"]["workers_online"] == 0
    assert result["now"]["disk_free_gb"] > 0
    assert result["now"]["catalog"]["active_links"] >= 1
    assert set(result["health"]) == {"daily", "last"}
    assert result["audit"]["loss_pct"] is None


def test_last_and_previous_runs_come_with_the_history_oldest_first(ingest):
    oldest = make_run(ingest, 30, persisted=1)
    middle = make_run(ingest, 20, persisted=2)
    newest = make_run(ingest, 10, persisted=4)

    result = build_panel(ingest, days=7)

    assert [r["run_id"] for r in result["runs"]] == [oldest, middle, newest]
    assert result["last"]["run_id"] == newest and result["last"]["persisted"] == 4
    assert result["previous"]["run_id"] == middle
    assert result["last"]["duration_s"] == 1800


def test_the_window_in_days_drops_older_runs(ingest):
    make_run(ingest, 24 * 9)
    recent = make_run(ingest, 5)
    assert [r["run_id"] for r in build_panel(ingest, days=7)["runs"]] == [recent]
    assert len(build_panel(ingest, days=30)["runs"]) == 2


def test_failures_are_merged_across_runs_and_sorted_by_count(ingest):
    make_run(ingest, 20, failed=3, reason="HTTP 403")
    make_run(ingest, 10, failed=2, reason="HTTP 403")
    make_run(ingest, 5, failed=1, reason="timed out")

    failures = build_panel(ingest, days=7)["failures"]

    assert failures[0] == {"stage": "fetch", "reason": "HTTP 403", "n": 5, "pct": 83.3}
    assert failures[1]["reason"] == "timed out" and failures[1]["n"] == 1


def test_the_audit_summary_estimates_the_loss_of_the_head_cut(ingest):
    run = make_run(ingest, 5)
    with ingest.connection() as db:
        job = db.execute(
            "SELECT id FROM ingest_jobs WHERE run_id=%s AND stage='discovery'", (run,)
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO ingest_attempts(job_id,attempt,owner,metrics) VALUES(%s,1,'t',%s)",
            (
                job,
                Jsonb(
                    {
                        "head_audit_sampled": 1,
                        "head_shadow_would_skip": 1,
                        "head_shadow_missed": 0,
                        "head_cut": 1,
                    }
                ),
            ),
        )
    audit = build_panel(ingest, days=7)["audit"]
    assert audit["sampled"] == 1 and audit["would_skip"] == 1 and audit["missed"] == 0
    assert audit["loss_pct"] == 0.0
    assert audit["cold_skipped"] == 2  # vem da config da última rodada


def test_the_audit_ignores_runs_where_the_cut_was_only_simulated(ingest):
    shadow = make_run(ingest, 20, head="shadow")
    with ingest.connection() as db:
        job = db.execute(
            "SELECT id FROM ingest_jobs WHERE run_id=%s AND stage='discovery'", (shadow,)
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO ingest_attempts(job_id,attempt,owner,metrics) VALUES(%s,1,'t',%s)",
            (job, Jsonb({"head_shadow_would_skip": 5, "head_shadow_missed": 2, "head_cut": 9})),
        )
    make_run(ingest, 5, head="enforce")

    audit = build_panel(ingest, days=7)["audit"]

    assert audit["would_skip"] == 0 and audit["missed"] == 0 and audit["loss_pct"] is None


def test_missing_stats_of_finished_runs_are_computed_on_demand_with_a_cap(ingest, monkeypatch):
    for hours in (40, 30, 20, 10, 5):
        make_run(ingest, hours)
    monkeypatch.setattr(panel, "MAX_ON_DEMAND", 3)

    first = build_panel(ingest, days=7)
    second = build_panel(ingest, days=7)

    assert len(first["runs"]) == 3 and first["pending_stats"] == 2
    assert len(second["runs"]) == 5 and second["pending_stats"] == 0


def test_an_open_run_is_recomputed_only_when_its_numbers_are_old(ingest):
    run = make_run(ingest, 1, finished=False)
    stats = refresh_run_stats(ingest, run)
    assert stats["final"] is False

    def computed_at():
        with ingest.connection() as db:
            return db.execute(
                "SELECT computed_at FROM ingest_run_stats WHERE run_id=%s", (run,)
            ).fetchone()["computed_at"]

    fresh = computed_at()
    build_panel(ingest, days=7)
    assert computed_at() == fresh  # números recentes: nada recalculado
    with ingest.connection() as db:
        db.execute(
            "UPDATE ingest_run_stats SET computed_at=now()-interval '5 minutes' WHERE run_id=%s",
            (run,),
        )
        db.execute(
            "UPDATE ingest_run_stats SET payload=jsonb_set(payload,'{computed_at}',"
            "to_jsonb((now()-interval '5 minutes')::text)) WHERE run_id=%s",
            (run,),
        )
    build_panel(ingest, days=7)
    assert computed_at() > fresh - timedelta(minutes=1) and computed_at() > datetime.now(
        UTC
    ) - timedelta(minutes=1)


def test_now_shows_open_queues_online_workers_and_the_catalog(ingest):
    make_run(ingest, 1, finished=False)
    with ingest.connection() as db:
        db.execute(
            "INSERT INTO ingest_workers(id,role,status) VALUES('a','worker','online'),"
            "('b','worker','online')"
        )
        db.execute(
            "INSERT INTO ingest_workers(id,role,status,heartbeat_at) "
            "VALUES('c','worker','online',now()-interval '1 hour')"
        )
    now = build_panel(ingest, days=7)["now"]

    assert {"stage": "fetch", "state": "queued", "n": 1} in now["queues"]
    assert now["workers_online"] == 2
    assert now["catalog"]["active_links"] >= 1 and "inactive_links" in now["catalog"]
    assert now["open_run"] is not None


def test_health_block_counts_actions_per_day_and_lists_the_last_runs(ingest):
    from buscador_midias.catalog.health_store import HealthStore

    HealthStore(ingest.store).initialize()
    with ingest.connection() as db:
        for action in ("health_inactivate", "health_inactivate", "health_reactivate"):
            db.execute(
                "INSERT INTO events(entity,entity_id,action,happened_at,before_json,after_json) "
                "VALUES('endpoint',1,%s,now(),'{}','{}')",
                (action,),
            )
    health = build_panel(ingest, days=7)["health"]

    today = health["daily"][-1]
    assert today["inactivated"] == 2 and today["reactivated"] == 1
    # Todos os dias da janela aparecem, mesmo sem nenhuma ação, para o eixo não pular datas.
    assert len(health["daily"]) == 7 and health["daily"][0]["inactivated"] == 0
    days = [d["day"] for d in health["daily"]]
    assert days == sorted(set(days))
    assert {"active", "recovery"} >= set(health["last"])


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 09h30 em Brasília -> as 12h
        (datetime(2026, 10, 29, 12, 30, tzinfo=UTC), datetime(2026, 10, 29, 15, 0, tzinfo=UTC)),
        # 17h -> 01h do dia seguinte
        (datetime(2026, 10, 29, 20, 0, tzinfo=UTC), datetime(2026, 10, 30, 4, 0, tzinfo=UTC)),
        # 00h30 -> 01h do mesmo dia
        (datetime(2026, 10, 30, 3, 30, tzinfo=UTC), datetime(2026, 10, 30, 4, 0, tzinfo=UTC)),
        # exatamente 12h: o próximo é o das 16h, nunca o horário que já chegou
        (datetime(2026, 10, 29, 15, 0, tzinfo=UTC), datetime(2026, 10, 29, 19, 0, tzinfo=UTC)),
    ],
)
def test_next_slot_is_the_next_fixed_hour_in_the_configured_zone(now, expected):
    assert next_slot(now, ZONE, [1, 6, 12, 16]) == expected


def test_next_slot_without_hours_is_none():
    assert next_slot(datetime.now(UTC), ZONE, []) is None


def call(ingest, query):
    return dispatch(
        SimpleNamespace(ingestion=ingest), SimpleNamespace(command="GET"), "/busca/painel", query
    )


def test_the_route_serves_the_panel_and_validates_the_window(ingest):
    status, body = call(ingest, {"days": "3"})
    assert status == 200 and body["days"] == 3
    assert call(ingest, {})[1]["days"] == 7
    for bad in ("0", "31", "x", "-1"):
        with pytest.raises(CatalogError):
            call(ingest, {"days": bad})


@pytest.fixture
def corpus(ingest):
    """Esquema temporário com a tabela do corpus e artigos de datas conhecidas."""
    name = "bm_test_corpus_" + uuid.uuid4().hex[:8]
    with ingest.connection() as db:
        db.execute(f'CREATE SCHEMA "{name}"')
        db.execute(
            f'CREATE TABLE "{name}".midias_ia_articles '
            "(id bigserial PRIMARY KEY, origin varchar, created_at timestamptz)"
        )
        now = datetime.now(UTC)
        for days_ago, origin, n in (
            (30, "imported_corpus", 100),  # antes da janela: só entra no total
            (2, "crawler", 1),
            (2, "live_search", 1),
            (1, "crawler", 1),
            (0, "crawler", 2),
            (0, "imported_corpus", 1),
        ):
            db.execute(
                f'INSERT INTO "{name}".midias_ia_articles(origin,created_at) '
                "SELECT %s,%s::timestamptz FROM generate_series(1,%s)",
                (origin, now - timedelta(days=days_ago, minutes=5), n),
            )
    yield name
    with ingest.connection() as db:
        db.execute(f'DROP SCHEMA "{name}" CASCADE')


def test_corpus_growth_is_the_running_total_per_day_with_what_entered_each_day(ingest, corpus):
    result = build_panel(ingest, days=3, corpus_schema=corpus)["corpus"]

    assert result["total"] == 106
    daily = result["daily"]
    assert len(daily) == 3 and [d["day"] for d in daily] == sorted(d["day"] for d in daily)
    assert [d["added"] for d in daily] == [2, 1, 3]
    assert [d["crawler"] for d in daily] == [1, 1, 2]
    assert [d["total"] for d in daily] == [102, 103, 106]  # o último fecha com o total
    assert result["total"] - sum(d["added"] for d in daily) == 100  # o que veio antes


def test_corpus_block_is_omitted_when_the_table_does_not_exist(ingest):
    assert build_panel(ingest, days=3, corpus_schema="bm_schema_inexistente")["corpus"] is None


def test_corpus_numbers_are_cached_between_requests(ingest, corpus, monkeypatch):
    build_panel(ingest, days=3, corpus_schema=corpus)
    with ingest.connection() as db:
        db.execute(
            f'INSERT INTO "{corpus}".midias_ia_articles(origin,created_at) '
            "VALUES('crawler',now())"
        )

    cached = build_panel(ingest, days=3, corpus_schema=corpus)["corpus"]
    assert cached["total"] == 106  # ainda o valor guardado

    monkeypatch.setattr(panel, "CORPUS_TTL", 0)
    panel._corpus_cache.clear()  # simula a expiração do que estava guardado
    assert build_panel(ingest, days=3, corpus_schema=corpus)["corpus"]["total"] == 107
