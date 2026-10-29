# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Leitura adaptativa: link que não rende há várias rodadas é lido menos, nunca abandonado."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest  # noqa: F401

from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion.cold_links import (
    MIN_READS,
    WINDOW_DAYS,
    cold_link_ids,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)
NOW = datetime.now(UTC)


def record(
    ingest, link_id, *, reads=MIN_READS + 1, candidates=0, failed=False, every_hours=6, newest=0
):
    """Leituras do link, da mais recente à mais antiga; os candidatos entram na ``newest``-ésima."""
    with ingest.connection() as db:
        for i in range(reads):
            at = NOW - timedelta(hours=2 + every_hours * i)
            run, poll = uuid.uuid4().hex, uuid.uuid4().hex
            db.execute(
                "INSERT INTO ingest_runs(id,trigger,state,since,until,config,created_at) "
                "VALUES(%s,'scheduled','completed',%s,%s,%s,%s)",
                (run, at - timedelta(hours=1), at, Jsonb({}), at),
            )
            db.execute(
                "INSERT INTO ingest_polls(id,run_id,link_id,snapshot,discovery_done,created_at) "
                "VALUES(%s,%s,%s,%s,true,%s)",
                (poll, run, link_id, Jsonb({}), at),
            )
            state = "failed" if failed and i == 0 else "succeeded"
            db.execute(
                "INSERT INTO ingest_jobs(id,stage,subject,run_id,poll_id,state) "
                "VALUES(%s,'discovery',%s,%s,%s,%s)",
                (uuid.uuid4().hex, "d-" + poll, run, poll, state),
            )
            for n in range(candidates if i == newest else 0):
                db.execute(
                    "INSERT INTO ingest_jobs(id,stage,subject,run_id,poll_id,state) "
                    "VALUES(%s,'dedup',%s,%s,%s,'succeeded')",
                    (uuid.uuid4().hex, f"n-{poll}-{n}", run, poll),
                )


def cold(ingest):
    with ingest.connection() as db:
        return cold_link_ids(db)


def test_a_link_read_often_without_a_single_candidate_is_cold(ingest):
    record(ingest, 1)
    assert cold(ingest) == {1}


def test_a_link_that_found_something_in_the_window_is_not_cold(ingest):
    record(ingest, 1, candidates=1)
    assert cold(ingest) == set()


def test_a_link_with_a_failed_discovery_keeps_being_read_so_it_can_recover(ingest):
    record(ingest, 1, failed=True)
    assert cold(ingest) == set()


def test_a_link_with_little_history_is_not_cold_yet(ingest):
    record(ingest, 1, reads=MIN_READS - 1)
    assert cold(ingest) == set()


def test_only_the_latest_reads_of_the_link_count_as_evidence(ingest):
    # Candidato só na quinta leitura mais recente: já saiu das últimas MIN_READS.
    record(ingest, 1, reads=MIN_READS + 3, candidates=1, newest=MIN_READS)
    assert cold(ingest) == {1}


def test_a_link_read_once_a_day_stays_cold_instead_of_flipping_back(ingest):
    record(ingest, 1, reads=MIN_READS + 1, every_hours=24)
    assert cold(ingest) == {1}


def test_reads_older_than_the_window_are_not_evidence(ingest):
    record(ingest, 1, reads=MIN_READS, every_hours=24 * (WINDOW_DAYS // MIN_READS + 2))
    assert cold(ingest) == set()


ZONE = "America/Sao_Paulo"


def at_slot(monkeypatch, hour):
    """Faz o relógio do agendador acreditar que a rodada é a das ``hour`` horas de hoje."""
    from buscador_midias.ingestion import store as store_module
    from buscador_midias.ingestion.flow import today_start

    slot = today_start(datetime.now(UTC), ZONE) + timedelta(hours=hour)
    monkeypatch.setattr(store_module, "clock_slot", lambda *args, **kwargs: slot)


def enable(ingest, mode="enforce", schedule_mode="clock"):
    ingest.configure(
        dict(
            version=ingest.settings()["version"],
            config=dict(
                enabled=True,
                schedule_mode=schedule_mode,
                schedule_hours=[1, 6, 12, 16],
                adaptive_polling=mode,
            ),
        )
    )


def polled_links(ingest, run):
    with ingest.connection() as db:
        rows = db.execute("SELECT link_id FROM ingest_polls WHERE run_id=%s", (run["id"],))
        return {r["link_id"] for r in rows.fetchall()}


def test_rounds_before_the_last_of_the_day_skip_cold_links(ingest, monkeypatch):
    record(ingest, 1)
    enable(ingest)
    at_slot(monkeypatch, 12)
    assert ingest.create_run({}, scheduled=True) is None  # o único link ativo está frio


def test_the_last_round_of_the_day_reads_cold_links_so_nothing_is_lost(ingest, monkeypatch):
    record(ingest, 1)
    enable(ingest)
    at_slot(monkeypatch, 16)
    assert polled_links(ingest, ingest.create_run({}, scheduled=True)) == {1}


def test_every_round_reads_everything_when_adaptive_polling_is_off(ingest, monkeypatch):
    record(ingest, 1)
    enable(ingest, mode="off")
    at_slot(monkeypatch, 12)
    assert polled_links(ingest, ingest.create_run({}, scheduled=True)) == {1}


def test_interval_scheduling_never_skips_links(ingest, monkeypatch):
    record(ingest, 1)
    enable(ingest, schedule_mode="link_interval")
    assert polled_links(ingest, ingest.create_run({}, scheduled=True)) == {1}


def test_manual_runs_always_read_cold_links(ingest):
    record(ingest, 1)
    enable(ingest)
    assert polled_links(ingest, ingest.create_run({})) == {1}


@pytest.mark.parametrize("value", ["shadow", True, None, 1])
def test_adaptive_polling_mode_is_validated(ingest, value):
    with pytest.raises(CatalogError):
        ingest.configure(
            dict(version=ingest.settings()["version"], config=dict(adaptive_polling=value))
        )


def test_adaptive_polling_is_off_by_default(ingest):
    assert ingest.settings()["config"]["adaptive_polling"] == "off"
