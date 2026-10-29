# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Fechamento do dia e nova tentativa de falhas: nada publicado depois da última rodada se perde."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest, next_job  # noqa: F401
from test_ingestion_cold_links import at_slot, enable, record

from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion.flow import is_boundary_slot, today_start

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)
ZONE = "America/Sao_Paulo"
HOURS = [1, 6, 12, 16]


def test_the_boundary_slots_are_the_first_and_the_last_of_the_day():
    now = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
    midnight = today_start(now, ZONE)
    flags = {
        hour: is_boundary_slot(midnight + timedelta(hours=hour), now, ZONE, HOURS) for hour in HOURS
    }
    assert flags == {1: True, 6: False, 12: False, 16: True}
    assert not is_boundary_slot(midnight + timedelta(hours=1), now, ZONE, [])


def run_since(ingest, monkeypatch, hour, **config):
    enable(ingest, mode="off")
    ingest.configure(
        dict(version=ingest.settings()["version"], config=dict(window_mode="today", **config))
    )
    at_slot(monkeypatch, hour)
    run = ingest.create_run({}, scheduled=True)
    return run["since"], today_start(datetime.now(UTC), ZONE)


def test_the_first_round_of_the_day_also_covers_the_previous_day_when_enabled(ingest, monkeypatch):
    since, midnight = run_since(ingest, monkeypatch, 1, close_previous_day=True)
    assert since == midnight - timedelta(days=1)


@pytest.mark.parametrize("hour", [6, 12, 16])
def test_the_other_rounds_keep_the_window_of_the_day(ingest, monkeypatch, hour):
    since, midnight = run_since(ingest, monkeypatch, hour, close_previous_day=True)
    assert since == midnight


def test_the_window_is_untouched_when_the_option_is_off(ingest, monkeypatch):
    since, midnight = run_since(ingest, monkeypatch, 1, close_previous_day=False)
    assert since == midnight


def test_a_manual_round_never_gets_the_extra_day(ingest):
    enable(ingest, mode="off")
    ingest.configure(
        dict(
            version=ingest.settings()["version"],
            config=dict(window_mode="today", close_previous_day=True),
        )
    )
    assert ingest.create_run({})["since"] == today_start(datetime.now(UTC), ZONE)


@pytest.mark.parametrize(("hour", "skipped"), [(1, False), (6, True), (12, True), (16, False)])
def test_cold_links_are_read_in_the_first_and_the_last_round_only(
    ingest, monkeypatch, hour, skipped
):
    record(ingest, 1)
    other = ingest.store.save(
        "endpoint", dict(url="https://jose.example/other.xml", kind="feed"), source_id=1
    )
    enable(ingest)
    at_slot(monkeypatch, hour)

    run = ingest.create_run({}, scheduled=True)

    with ingest.connection() as db:
        read = {
            r["link_id"]
            for r in db.execute("SELECT link_id FROM ingest_polls WHERE run_id=%s", (run["id"],))
        }
    assert read == ({other["id"]} if skipped else {1, other["id"]})


@pytest.mark.parametrize("value", ["sim", 1, 0, None])
def test_close_previous_day_must_be_a_boolean(ingest, value):
    with pytest.raises(CatalogError):
        ingest.configure(
            dict(version=ingest.settings()["version"], config=dict(close_previous_day=value))
        )


@pytest.mark.parametrize("value", [0, 6, "3", True, None, 2.5])
def test_failed_retry_limit_is_validated(ingest, value):
    with pytest.raises(CatalogError):
        ingest.configure(
            dict(version=ingest.settings()["version"], config=dict(failed_retry_limit=value))
        )


def test_the_new_options_are_off_by_default(ingest):
    config = ingest.settings()["config"]
    assert config["close_previous_day"] is False and config["failed_retry_limit"] == 1


# ---- nova tentativa de notícias que falharam ----

URL = "https://jose.example/noticia"


def fail_in_a_round(ingest, reason):
    """Rodada em que a notícia é descoberta, passa pelo dedup e falha na extração."""
    run = ingest.create_run({})
    ingest.finish(next_job(ingest, "discovery"), dict(candidates=[dict(url=URL)]))
    ingest.finish(next_job(ingest, "dedup"), dict(next="fetch"))
    ingest.failure(next_job(ingest, "fetch"), reason)
    ingest.maintain()
    return run


def rediscover(ingest):
    """Nova rodada em que o mesmo feed volta a listar a notícia; devolve as tarefas de dedup."""
    run = ingest.create_run({})
    ingest.finish(next_job(ingest, "discovery"), dict(candidates=[dict(url=URL)]))
    with ingest.connection() as db:
        return db.execute(
            "SELECT generation FROM ingest_jobs WHERE run_id=%s AND stage='dedup'", (run["id"],)
        ).fetchall()


def configure(ingest, **config):
    ingest.configure(
        dict(version=ingest.settings()["version"], config=dict(max_attempts=1, **config))
    )


def test_a_failed_article_found_again_in_a_later_round_is_tried_again(ingest):
    configure(ingest, failed_retry_limit=3)
    fail_in_a_round(ingest, "The read operation timed out")

    again = rediscover(ingest)

    assert [row["generation"] for row in again] == [2]


def test_it_stays_failed_when_the_retry_limit_is_one(ingest):
    configure(ingest, failed_retry_limit=1)
    fail_in_a_round(ingest, "The read operation timed out")
    assert rediscover(ingest) == []


@pytest.mark.parametrize(
    "reason",
    [
        "HTTP 403 na notícia",
        "HTTP 401 na notícia",
        "HTTP 404 na notícia",
        "redirect_cycle: ciclo de redirecionamento",
        "Destino fora dos domínios do site e do mecanismo.",
        "bright_data_api_error: proxy HTTP 407",
    ],
)
def test_blocks_and_definitive_failures_are_not_retried_across_rounds(ingest, reason):
    configure(ingest, failed_retry_limit=3)
    fail_in_a_round(ingest, reason)
    assert rediscover(ingest) == []


def test_retries_stop_at_the_configured_number_of_passes(ingest):
    configure(ingest, failed_retry_limit=2)
    fail_in_a_round(ingest, "The read operation timed out")
    assert [r["generation"] for r in rediscover(ingest)] == [2]
    ingest.failure(next_job(ingest, "dedup"), "The read operation timed out")  # falha de novo
    ingest.maintain()
    assert rediscover(ingest) == []  # já foram 2 passadas


def test_the_same_round_never_retries_its_own_failure(ingest):
    configure(ingest, failed_retry_limit=3)
    ingest.store.save("endpoint", dict(url="https://jose.example/second", kind="feed"), source_id=1)
    run = ingest.create_run({})
    ingest.finish(next_job(ingest, "discovery"), dict(candidates=[dict(url=URL)]))
    ingest.finish(next_job(ingest, "dedup"), dict(next="fetch"))
    ingest.failure(next_job(ingest, "fetch"), "The read operation timed out")
    # Outro feed da MESMA rodada lista a mesma notícia: não é uma rodada nova, não repete.
    ingest.finish(next_job(ingest, "discovery"), dict(candidates=[dict(url=URL)]))
    with ingest.connection() as db:
        dedups = db.execute(
            "SELECT count(*) n FROM ingest_jobs WHERE run_id=%s AND stage='dedup'", (run["id"],)
        ).fetchone()["n"]
    assert dedups == 1
