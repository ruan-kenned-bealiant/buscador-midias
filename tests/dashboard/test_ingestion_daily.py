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
    slot = clock_slot(datetime.fromisoformat(stamp), "America/Sao_Paulo", [6, 12, 16])
    assert (slot.hour if slot else None) == hour
