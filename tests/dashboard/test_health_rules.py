"""Regras da rotina de saúde: ação por resultado da sonda e próximo horário fixo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.catalog.health_rules import decide, next_run, recovery_due


def outcome(proposal, **extra):
    base = dict(proposal=proposal, reason="", link_id=1, url="https://a.example/feed")
    return {**base, **extra}


def test_a_working_link_stays_and_clears_its_failures():
    found = decide(outcome("keep", age_days=2), previous_failures=1)
    assert (found.action, found.category, found.failures) == ("keep", "healthy", 0)


def test_a_link_stopped_for_over_a_month_is_inactivated():
    found = decide(outcome("inactivate", age_days=45), previous_failures=0)
    assert (found.action, found.category) == ("inactivate", "stale")
    assert "45" in found.reason


@pytest.mark.parametrize("proposal", ["delete", "remove_theme", "remove_historical"])
def test_a_year_old_historical_or_useless_theme_link_is_removed(proposal):
    found = decide(outcome(proposal, reason="motivo"), previous_failures=0)
    assert (found.action, found.category) == ("delete", "removed")


@pytest.mark.parametrize(
    "extra",
    [
        dict(cause="access", status=404),
        dict(cause="access", status=410),
        dict(cause="access", reason="acesso: redirect_cycle: ciclo"),
    ],
)
def test_a_link_that_is_gone_for_good_is_inactivated_at_once(extra):
    found = decide(outcome("review", **extra), previous_failures=0)
    assert (found.action, found.category, found.failures) == ("inactivate", "access_error", 1)


@pytest.mark.parametrize(
    "extra",
    [
        dict(cause="access", status=503, transient=True),
        dict(cause="access", status=403),
        dict(cause="access", status=402),
        dict(cause="access", status=None, transient=True),
        dict(cause="unreadable"),
    ],
)
def test_a_transient_failure_is_only_recorded_the_first_time(extra):
    found = decide(outcome("review", **extra), previous_failures=0)
    assert (found.action, found.failures) == ("review", 1)


@pytest.mark.parametrize("cause", ["access", "unreadable"])
def test_the_second_failure_in_a_row_inactivates(cause):
    found = decide(outcome("review", cause=cause, status=503, transient=True), previous_failures=1)
    assert (found.action, found.failures) == ("inactivate", 2)


@pytest.mark.parametrize("cause", ["undated", "sports"])
def test_cases_for_human_review_never_change_the_link_nor_count_as_failures(cause):
    found = decide(outcome("review", cause=cause), previous_failures=1)
    assert (found.action, found.category, found.failures) == ("review", "review", 0)


def test_next_run_uses_the_next_clock_slot_in_brasilia():
    now = datetime(2026, 10, 28, 20, 30, tzinfo=UTC)  # 17h30 em Brasília
    assert next_run(now, [10, 20], 1440) == datetime(2026, 10, 28, 23, 0, tzinfo=UTC)
    late = datetime(2026, 10, 28, 23, 0, 1, tzinfo=UTC)  # 20h00 e 1 s em Brasília
    assert next_run(late, [10, 20], 1440) == datetime(2026, 10, 29, 13, 0, tzinfo=UTC)


def test_next_run_wraps_to_the_first_slot_of_the_next_day():
    now = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)  # 23h de 28/10 em Brasília
    assert next_run(now, [20, 10], 1440) == datetime(2026, 10, 29, 13, 0, tzinfo=UTC)


def test_without_slots_the_interval_is_used():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert next_run(now, [], 90) == datetime(2026, 10, 28, 13, 30, tzinfo=UTC)


NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("changed_days_ago", "checked_days_ago", "expected"),
    [
        (1, 1, True),  # caiu ontem: volta a ser testado a cada rodada, é onde há recuperação
        (6.9, 1, True),
        (30, 1, False),  # parado há semanas e testado ontem: não repete todo dia
        (30, 6.9, False),
        (30, 7.1, True),  # varredura semanal dos antigos
        (30, None, True),  # nunca verificado pela saúde
    ],
)
def test_recovery_retests_recent_failures_every_round_and_old_ones_weekly(
    changed_days_ago, checked_days_ago, expected
):
    changed = NOW - timedelta(days=changed_days_ago)
    checked = None if checked_days_ago is None else NOW - timedelta(days=checked_days_ago)
    assert recovery_due(changed, checked, NOW) is expected
