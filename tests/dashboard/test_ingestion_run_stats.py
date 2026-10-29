# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Estatísticas pré-agregadas por rodada: o painel lê números prontos, não varre as tarefas."""

from __future__ import annotations

import os

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest  # noqa: F401
from test_ingestion_cold_links import record
from test_ingestion_fast_batch import prepare
from test_ingestion_report_indexes import run_with_metrics

from buscador_midias.ingestion.run_stats import (
    backfill_stats,
    finalize_pending,
    load_stats,
    refresh_run_stats,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def finish(ingest, run_id):
    with ingest.connection() as db:
        db.execute(
            "UPDATE ingest_runs SET finished_at=now(),state='partial' WHERE id=%s", (run_id,)
        )
        db.execute("UPDATE ingest_polls SET discovery_done=true WHERE run_id=%s", (run_id,))


def test_stats_summarize_stages_failures_cost_and_occurrences(ingest):
    run = run_with_metrics(ingest)

    stats = refresh_run_stats(ingest, run["id"])

    with ingest.connection() as db:
        oracle = {
            (r["stage"], r["state"]): r["n"]
            for r in db.execute(
                "SELECT stage,state,count(*) n FROM ingest_jobs WHERE run_id=%s GROUP BY 1,2",
                (run["id"],),
            ).fetchall()
        }
    for (stage, state), n in oracle.items():
        assert stats["stages"][stage][state] == n
    assert stats["occurrences"] == 2 and stats["unique_articles"] == 2
    assert stats["links"] == 1 and stats["links_done"] == 0
    assert stats["cost_usd"] == pytest.approx(0.01) and stats["tokens_in"] == 5
    assert stats["persisted"] == 0
    assert {"stage": "dedup", "reason": "falha simulada", "n": 1} in stats["failures"]
    assert stats["final"] is False and stats["state"] == "running"
    assert stats["head_filter"] == "shadow"  # o padrão da configuração da rodada
    assert stats["duration_s"] >= 0


def test_refreshing_twice_keeps_one_row_and_marks_a_finished_run_final(ingest):
    run = run_with_metrics(ingest)
    refresh_run_stats(ingest, run["id"])
    finish(ingest, run["id"])
    again = refresh_run_stats(ingest, run["id"])

    assert again["final"] is True and again["state"] == "partial"
    with ingest.connection() as db:
        rows = db.execute("SELECT count(*) n FROM ingest_run_stats").fetchone()["n"]
    assert rows == 1
    assert load_stats(ingest, [run["id"]])[run["id"]]["final"] is True


def test_finalize_pending_computes_only_finished_runs_without_final_stats(ingest):
    run = run_with_metrics(ingest)
    assert finalize_pending(ingest) == []  # aberta: nada a fechar
    finish(ingest, run["id"])

    assert finalize_pending(ingest, limit=1) == [run["id"]]
    assert finalize_pending(ingest) == []  # já fechada


def test_audit_counters_are_summed_from_the_discovery_attempts(ingest):
    run, _ = prepare(ingest, 1)
    with ingest.connection() as db:
        db.execute(
            "UPDATE ingest_attempts SET metrics=%s WHERE job_id IN "
            "(SELECT id FROM ingest_jobs WHERE run_id=%s AND stage='discovery')",
            (
                '{"head_audit_sampled":1,"head_shadow_would_skip":1,"head_shadow_missed":1,'
                '"head_cut":1,"head_stopped_download":1,"not_modified_responses":1}',
                run["id"],
            ),
        )
    audit = refresh_run_stats(ingest, run["id"])["audit"]
    assert audit["sampled"] == 1 and audit["would_skip"] == 1 and audit["missed"] == 1
    assert audit["head_cut"] == 1 and audit["not_modified"] == 1
    assert audit["discovery_reads"] >= 1


def test_the_stats_table_exists_on_new_schemas_and_ensure_recreates_it(ingest):
    with ingest.connection() as db:
        db.execute("DROP TABLE ingest_run_stats")
    assert "ingest_run_stats" in ingest.ensure_report_indexes()
    assert ingest.ensure_report_indexes() == []
    run = run_with_metrics(ingest)
    assert refresh_run_stats(ingest, run["id"])["occurrences"] == 2


def test_a_run_records_how_many_cold_links_it_skipped(ingest, monkeypatch):
    from test_ingestion_cold_links import at_slot, enable

    record(ingest, 1)
    other = ingest.store.save(
        "endpoint", dict(url="https://jose.example/other.xml", kind="feed"), source_id=1
    )
    assert other["id"] != 1
    enable(ingest)
    at_slot(monkeypatch, 12)

    run = ingest.create_run({}, scheduled=True)

    assert run["config"]["adaptive_skipped"] == 1
    assert refresh_run_stats(ingest, run["id"])["adaptive_skipped"] == 1


def test_load_stats_returns_only_what_exists(ingest):
    run = run_with_metrics(ingest)
    assert load_stats(ingest, [run["id"], "x" * 32]) == {}
    refresh_run_stats(ingest, run["id"])
    assert set(load_stats(ingest, [run["id"], "x" * 32])) == {run["id"]}


def test_backfill_computes_every_finished_run_in_the_window_without_final_stats(ingest):
    done = run_with_metrics(ingest)["id"]
    finish(ingest, done)
    open_run = ingest.create_run({})
    with ingest.connection() as db:
        db.execute(
            "UPDATE ingest_runs SET created_at=now()-interval '40 days' WHERE id=%s", (done,)
        )

    assert backfill_stats(ingest, days=7) == []  # a rodada terminada é antiga demais
    assert backfill_stats(ingest, days=60) == [done]  # a aberta nunca entra: ainda muda
    assert backfill_stats(ingest, days=60) == []
    assert backfill_stats(ingest, days=60, force=True) == [done]  # recalcula o que já existe
    assert open_run["id"] not in load_stats(ingest, [open_run["id"]])
