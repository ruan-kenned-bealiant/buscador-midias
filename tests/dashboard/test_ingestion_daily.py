"""Janela civil e roteamento sem expansão de arquivos sem indicação temporal."""

from datetime import UTC, datetime

import pytest

from buscador_midias.ingestion.flow import clock_slot, today_start


@pytest.mark.parametrize(
    "now,expected",
    [
        ("2026-10-24T02:59:59+00:00", "2026-10-23T00:00:00-03:00"),
        ("2026-10-24T03:00:00+00:00", "2026-10-24T00:00:00-03:00"),
        ("2026-10-24T23:59:00+00:00", "2026-10-24T00:00:00-03:00"),
    ],
)
def test_day_starts_at_local_midnight(now, expected):
    assert today_start(datetime.fromisoformat(now)).isoformat() == expected


def test_utc_timezone_and_naive_dates():
    assert today_start(datetime(2026, 10, 24, 2, tzinfo=UTC), "UTC").hour == 0
    with pytest.raises(ValueError):
        today_start(datetime(2026, 10, 24))


@pytest.mark.parametrize(
    "stamp,hour",
    [
        ("2026-10-24T00:59:59-03:00", None),
        ("2026-10-24T01:00:00-03:00", 1),
        ("2026-10-24T01:15:00-03:00", None),
        ("2026-10-24T05:59:59-03:00", None),
        ("2026-10-24T06:00:00-03:00", 6),
        ("2026-10-24T06:14:59-03:00", 6),
        ("2026-10-24T06:15:00-03:00", None),
        ("2026-10-24T12:00:00-03:00", 12),
        ("2026-10-24T16:00:00-03:00", 16),
        ("2026-10-25T00:00:00-03:00", None),
    ],
)
def test_clock_slots_do_not_replay_missed_runs(stamp, hour):
    slot = clock_slot(datetime.fromisoformat(stamp), "America/Sao_Paulo", [1, 6, 12, 16])
    assert (slot.hour if slot else None) == hour


NOW = datetime(2026, 10, 26, 15, 30, tzinfo=UTC)  # 12h30 em Brasília


def test_past_day_covers_the_whole_civil_day_in_the_configured_zone():
    from buscador_midias.ingestion.flow import day_window

    since, until = day_window("2026-10-24", NOW, "America/Sao_Paulo")
    assert since.isoformat() == "2026-10-24T00:00:00-03:00"
    assert until.isoformat() == "2026-10-25T00:00:00-03:00"
    since, until = day_window("2026-10-24", NOW, "UTC")
    assert since.isoformat() == "2026-10-24T00:00:00+00:00"
    assert until.isoformat() == "2026-10-25T00:00:00+00:00"


def test_current_day_ends_now_instead_of_in_the_future():
    from buscador_midias.ingestion.flow import day_window

    since, until = day_window("2026-10-26", NOW, "America/Sao_Paulo")
    assert since.isoformat() == "2026-10-26T00:00:00-03:00"
    assert until == NOW


@pytest.mark.parametrize(
    "day",
    ["2026-10-27", "2026-09-25", "26/10/2026", "2026-13-01", "", "ontem", None, 20261026],
)
def test_day_must_be_valid_not_future_and_within_the_limit(day):
    from buscador_midias.ingestion.flow import day_window

    with pytest.raises(ValueError):
        day_window(day, NOW, "America/Sao_Paulo")


def test_oldest_accepted_day_is_thirty_days_back():
    from buscador_midias.ingestion.flow import day_window

    assert day_window("2026-09-26", NOW, "America/Sao_Paulo")[0].day == 26
    with pytest.raises(ValueError):
        day_window("2026-09-25", NOW, "America/Sao_Paulo")
