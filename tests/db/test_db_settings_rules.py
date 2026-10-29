"""Validação pura das configurações (sem banco): limites e valores aceitos."""

from __future__ import annotations

import pytest

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.settings import (
    SEARCH_DEFAULTS,
    STAGES,
    health_defaults,
    validate_health,
    validate_search_patch,
)


def test_defaults_are_complete_and_valid():
    assert set(STAGES) == set(SEARCH_DEFAULTS["workers"])
    assert validate_search_patch(dict(SEARCH_DEFAULTS)) == SEARCH_DEFAULTS


@pytest.mark.parametrize(
    "patch",
    [
        {"desconhecida": 1},
        {"enabled": "sim"},
        {"schedule_mode": "aleatorio"},
        {"schedule_hours": [1, 1]},
        {"schedule_hours": [24]},
        {"schedule_hours": []},
        {"window_mode": "ontem"},
        {"timezone": "Mars/Base"},
        {"workers": {"fetch": 2}},
        {"workers": {**SEARCH_DEFAULTS["workers"], "fetch": 0}},
        {"content_filter": "talvez"},
        {"head_filter": 1},
        {"close_previous_day": "true"},
        {"adaptive_polling": "on"},
        {"head_audit_rate": 1.5},
        {"head_audit_rate": True},
        {"blocked_content_categories": ["news"]},
        {"blocked_content_categories": ["sports", "sports"]},
        {"model": "modelo com espaço"},
        {"max_attempts": 4},
        {"host_concurrency": 5},
        {"failed_retry_limit": 0},
        {"bright_data": True},
    ],
)
def test_invalid_search_values_are_rejected(patch):
    with pytest.raises(CatalogError):
        validate_search_patch(patch)


def test_valid_patch_is_returned_unchanged():
    patch = dict(
        schedule_hours=[1, 6, 12, 16],
        head_audit_rate=0.02,
        close_previous_day=True,
        failed_retry_limit=3,
        host_concurrency_max=3,
        model="openai/gpt-4.1-mini",
    )
    assert validate_search_patch(patch) == patch


@pytest.mark.parametrize("scope", ["health_active", "health_recovery"])
def test_health_defaults_are_valid_for_their_scope(scope):
    values = health_defaults(scope)
    assert validate_health(scope, values) == values


def test_health_hours_are_sorted():
    values = {**health_defaults("health_active"), "schedule_hours": [20, 10]}
    assert validate_health("health_active", values)["schedule_hours"] == [10, 20]


@pytest.mark.parametrize(
    ("scope", "change"),
    [
        ("health_active", {"enabled": 1}),
        ("health_active", {"workers": 33}),
        ("health_active", {"max_attempts": 0}),
        ("health_active", {"interval_minutes": 0}),
        ("health_active", {"max_bright_requests": 49}),
        ("health_active", {"schedule_hours": [10, 10]}),
        ("health_active", {"mode": "quick"}),
        ("health_active", {"scope": "inactive"}),
        ("health_recovery", {"mode": "probe"}),
        ("health_recovery", {"scope": "everything"}),
        ("health_other", {}),
    ],
)
def test_invalid_health_values_are_rejected(scope, change):
    base = health_defaults(scope) if scope != "health_other" else health_defaults("health_active")
    with pytest.raises(CatalogError):
        validate_health(scope, {**base, **change})
