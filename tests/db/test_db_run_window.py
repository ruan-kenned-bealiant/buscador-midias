"""Janela de uma rodada de busca: dia de hoje, período, dia civil e fechamento do dia anterior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.runs import search_window

HOURS = [1, 6, 12, 16]
TODAY = dict(window_mode="today", timezone="America/Sao_Paulo", schedule_hours=HOURS)
NOW = datetime(2026, 10, 29, 15, 5, tzinfo=UTC)  # 12:05 em Brasília
MIDNIGHT = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)  # 00:00 em Brasília


def test_default_window_runs_from_local_midnight_until_now():
    assert search_window(TODAY, {}, NOW) == (MIDNIGHT, NOW)


def test_lookback_mode_goes_back_by_the_configured_minutes():
    config = {**TODAY, "window_mode": "lookback", "lookback_minutes": 120}
    assert search_window(config, {}, NOW) == (NOW - timedelta(minutes=120), NOW)


def test_a_civil_day_is_resolved_in_local_time():
    since, until = search_window(TODAY, {"day": "2026-10-28"}, NOW)
    assert since == datetime(2026, 10, 28, 3, 0, tzinfo=UTC)
    assert until == datetime(2026, 10, 29, 3, 0, tzinfo=UTC)


def test_an_explicit_period_is_respected():
    since = MIDNIGHT + timedelta(hours=2)
    until = MIDNIGHT + timedelta(hours=5)
    assert search_window(TODAY, {"since": since.isoformat(), "until": until.isoformat()}, NOW) == (
        since,
        until,
    )


@pytest.mark.parametrize(
    "data",
    [
        {"day": "2026-10-28", "since": "2026-10-28T10:00:00+00:00"},
        {"day": "28/10/2026"},
        {"day": "2020-01-01"},
        {"since": "2026-10-29T10:00:00+00:00", "until": "2026-10-29T10:00:00+00:00"},
        {"since": "2026-10-29T10:00:00", "until": "2026-10-29T11:00:00+00:00"},
        {"until": "2026-10-30T10:00:00+00:00"},
        {"since": "nao-e-data"},
        {"since": "2026-10-28T10:00:00+00:00", "until": "2026-10-28T12:00:00+00:00"},
    ],
)
def test_invalid_windows_are_rejected(data):
    with pytest.raises(CatalogError):
        search_window(TODAY, data, NOW)


def test_a_past_period_is_allowed_outside_today_mode():
    config = {**TODAY, "window_mode": "lookback", "lookback_minutes": 60}
    since = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    until = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
    assert search_window(config, {"since": since.isoformat(), "until": until.isoformat()}, NOW) == (
        since,
        until,
    )


FIRST = MIDNIGHT + timedelta(hours=1)  # 01:00 em Brasília
NOW_FIRST = FIRST + timedelta(minutes=5)


def test_the_first_round_of_the_day_also_covers_the_previous_day_when_enabled():
    config = {**TODAY, "close_previous_day": True}
    since, until = search_window(config, {}, NOW_FIRST, slot=FIRST)
    assert since == MIDNIGHT - timedelta(days=1) and until == NOW_FIRST


@pytest.mark.parametrize("hour", [6, 12, 16])
def test_the_other_rounds_keep_the_window_of_the_day(hour):
    config = {**TODAY, "close_previous_day": True}
    slot = MIDNIGHT + timedelta(hours=hour)
    assert search_window(config, {}, slot + timedelta(minutes=5), slot=slot)[0] == MIDNIGHT


def test_the_window_is_untouched_when_the_option_is_off():
    assert search_window(TODAY, {}, NOW_FIRST, slot=FIRST)[0] == MIDNIGHT


def test_a_manual_round_never_gets_the_extra_day():
    config = {**TODAY, "close_previous_day": True}
    assert search_window(config, {}, NOW_FIRST)[0] == MIDNIGHT


def test_a_chosen_day_never_gets_the_extra_day():
    config = {**TODAY, "close_previous_day": True}
    since, _ = search_window(config, {"day": "2026-10-29"}, NOW_FIRST, slot=FIRST)
    assert since == MIDNIGHT
