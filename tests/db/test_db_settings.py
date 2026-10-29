"""Configurações em ``bm_settings``: uma linha por escopo, revisão otimista e agenda."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.settings import SEARCH_DEFAULTS, SettingsRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.fixture
def settings(schema_name: str) -> SettingsRepository:
    repository = SettingsRepository(schema_name)
    repository.install()
    return repository


def test_install_creates_the_three_scopes_once(settings):
    settings.install()
    scopes = [row["scope"] for row in settings.all()]
    assert scopes == ["health_active", "health_recovery", "search"]
    assert settings.search()["config"] == SEARCH_DEFAULTS
    assert settings.search()["version"] == 1


def test_install_keeps_values_already_saved(settings):
    current = settings.search()
    settings.configure_search(dict(version=current["version"], config=dict(model="x/y")))
    settings.install()
    assert settings.search()["config"]["model"] == "x/y"


def test_search_configuration_is_versioned(settings):
    current = settings.search()
    saved = settings.configure_search(
        dict(version=current["version"], config=dict(close_previous_day=True, failed_retry_limit=3))
    )
    assert saved["version"] == current["version"] + 1
    assert saved["config"]["close_previous_day"] is True
    assert saved["config"]["model"] == SEARCH_DEFAULTS["model"]
    with pytest.raises(CatalogError) as stale:
        settings.configure_search(dict(version=current["version"], config=dict(enabled=True)))
    assert stale.value.status == 409


@pytest.mark.parametrize(
    "data",
    [
        dict(config=dict(enabled=True)),
        dict(version="1", config=dict(enabled=True)),
        dict(version=1, config=dict(enabled="sim")),
        dict(version=1, config=dict(enabled=True), extra=1),
    ],
)
def test_search_configuration_rejects_malformed_requests(settings, data):
    with pytest.raises(CatalogError):
        settings.configure_search(data)
    assert settings.search()["version"] == 1


def test_changes_are_audited_as_a_diff(settings):
    current = settings.search()
    settings.configure_search(dict(version=current["version"], config=dict(retry_minutes=5)))
    with settings.connection() as db:
        event = db.execute(
            "SELECT entity,actor,changes FROM bm_events WHERE entity='settings' ORDER BY id DESC"
        ).fetchone()
    assert event["changes"]["scope"] == "search"
    assert event["changes"]["retry_minutes"] == [1, 5]
    assert "model" not in event["changes"]


def test_health_configuration_schedules_the_next_fixed_hour(settings):
    current = settings.health("health_active")
    before = datetime.now(UTC)
    saved = settings.configure_health(
        "health_active",
        dict(
            version=current["version"],
            schedule_hours=[10, 20],
            enabled=True,
            interval_minutes=1440,
            workers=16,
            max_attempts=2,
            mode="probe",
        ),
    )
    assert saved["version"] == current["version"] + 1
    assert saved["config"]["schedule_hours"] == [10, 20]
    assert saved["config"]["workers"] == 16
    assert before < saved["next_run_at"] <= before + timedelta(hours=24)


def test_health_next_run_is_kept_when_only_other_fields_change(settings):
    first = settings.configure_health("health_active", dict(version=1, schedule_hours=[10, 20]))
    second = settings.configure_health("health_active", dict(version=first["version"], workers=4))
    assert second["next_run_at"] == first["next_run_at"]
    assert second["config"]["workers"] == 4


@pytest.mark.parametrize(
    ("scope", "data"),
    [
        ("health_active", dict(version=1, mode="quick")),
        ("health_recovery", dict(version=1, mode="probe")),
        ("health_active", dict(version=1, surprise=True)),
        ("health_active", dict(mode="full")),
        ("search", dict(version=1)),
    ],
)
def test_health_configuration_rejects_invalid_requests(settings, scope, data):
    with pytest.raises(CatalogError):
        settings.configure_health(scope, data)


def test_health_configuration_conflicts_on_stale_version(settings):
    settings.configure_health("health_recovery", dict(version=1, workers=4))
    with pytest.raises(CatalogError) as stale:
        settings.configure_health("health_recovery", dict(version=1, workers=5))
    assert stale.value.status == 409


def test_bright_data_cannot_be_enabled_without_credentials(settings, monkeypatch):
    monkeypatch.setattr("buscador_midias.db.settings.bright_data_available", lambda: False)
    with pytest.raises(CatalogError):
        settings.configure_health("health_active", dict(version=1, bright_data=True))
