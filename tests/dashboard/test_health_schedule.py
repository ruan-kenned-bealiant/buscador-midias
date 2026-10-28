# ruff: noqa: F811 -- fixtures importadas de test_catalog_health_postgres são reutilizadas por nome.
"""Rotina de saúde por horários fixos, com modo e escopo configuráveis."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from test_catalog_health_postgres import health  # noqa: F401
from test_catalog_postgres import local, schema  # noqa: F401

from buscador_midias.catalog.store import CatalogError

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def configure(health, kind="active", **changes):
    current = next(s for s in health.settings() if s["kind"] == kind)
    data = dict(
        enabled=True,
        interval_minutes=1440,
        workers=8,
        max_attempts=2,
        version=current["version"],
    )
    return health.configure(kind, {**data, **changes})


def test_new_settings_default_to_the_old_behaviour(health):
    for setting in health.settings():
        assert setting["schedule_hours"] == [] and setting["mode"] == "full"
        assert setting["scope"] == "errors"


def test_clock_mode_and_scope_can_be_configured(health):
    active = configure(health, "active", schedule_hours=[20, 10], mode="probe")
    assert sorted(active["schedule_hours"]) == [10, 20] and active["mode"] == "probe"
    recovery = configure(health, "recovery", schedule_hours=[10, 20], scope="inactive")
    assert recovery["scope"] == "inactive"


@pytest.mark.parametrize(
    "kind, changes",
    [
        ("active", dict(schedule_hours=[24])),
        ("active", dict(schedule_hours=[-1])),
        ("active", dict(schedule_hours=[10, 10])),
        ("active", dict(schedule_hours="10")),
        ("active", dict(schedule_hours=[True])),
        ("active", dict(mode="rapido")),
        ("recovery", dict(mode="probe")),
        ("active", dict(scope="inactive")),
        ("recovery", dict(scope="tudo")),
    ],
)
def test_invalid_clock_mode_or_scope_is_refused(health, kind, changes):
    with pytest.raises(CatalogError):
        configure(health, kind, **changes)


def test_configuring_slots_moves_the_next_run_to_the_next_slot(health):
    after = configure(health, "active", schedule_hours=[10, 20])
    local_time = after["next_run_at"].astimezone(ZoneInfo("America/Sao_Paulo"))
    assert local_time.hour in (10, 20) and local_time.minute == 0
    assert after["next_run_at"] > datetime.now(UTC)


def test_a_scheduled_run_schedules_the_following_slot_not_a_fixed_interval(health):
    configure(health, "active", schedule_hours=[10, 20])
    with health.connection() as db:
        db.execute("UPDATE health_settings SET next_run_at=now()-interval '1 minute'")
    assert health.create("active", "scheduled") is not None
    after = next(s for s in health.settings() if s["kind"] == "active")
    local_time = after["next_run_at"].astimezone(ZoneInfo("America/Sao_Paulo"))
    assert local_time.hour in (10, 20) and after["next_run_at"] > datetime.now(UTC)


def test_the_scheduled_recovery_with_inactive_scope_tests_every_inactive_link(health):
    link = health.store.endpoint(1)
    health.store.save("endpoint", dict(version=link["version"], active=False), 1)
    configure(health, "recovery", scope="inactive")
    with health.connection() as db:
        db.execute("UPDATE health_settings SET next_run_at=now()-interval '1 minute'")
    run = health.create("recovery", "scheduled")
    assert run["total"] == 1


def test_finishing_a_run_keeps_the_next_clock_slot_instead_of_adding_a_whole_interval(health):
    from datetime import timedelta

    from test_catalog_health_postgres import execute

    configure(health, "active", schedule_hours=[10, 20])
    with health.connection() as db:
        db.execute("UPDATE health_settings SET next_run_at=now()-interval '1 minute'")
    health.create("active", "scheduled")
    execute(health)
    after = next(s for s in health.settings() if s["kind"] == "active")
    assert after["next_run_at"] - datetime.now(UTC) <= timedelta(hours=14, minutes=1)
