"""Regras da rotina de saúde: ação por resultado da sonda e próximo horário fixo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.catalog.health_rules import (
    FAILURES_TO_INACTIVATE,
    TRANSIENT_FAILURES_TO_INACTIVATE,
    decide,
    next_run,
    recovery_due,
)
from buscador_midias.catalog.link_review import _transient


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


@pytest.mark.parametrize(
    "extra",
    [
        dict(cause="access", status=403),
        dict(cause="access", status=406),
        dict(cause="access", status=401),
        dict(cause="unreadable", status=200),
    ],
)
def test_a_refusal_or_unreadable_document_inactivates_on_the_second_failure_in_a_row(extra):
    found = decide(outcome("review", **extra), previous_failures=FAILURES_TO_INACTIVATE - 1)
    assert (found.action, found.failures) == ("inactivate", FAILURES_TO_INACTIVATE)


@pytest.mark.parametrize(
    "extra",
    [
        dict(cause="access", status=429, transient=True),
        dict(cause="access", status=503, transient=True),
        dict(cause="access", status=None, transient=True, reason="acesso: timed out"),
    ],
)
def test_a_failure_that_passes_by_itself_does_not_inactivate_on_the_second_in_a_row(extra):
    """429, 5xx e timeout são do site (ou nossos) naquela hora; o link só sai se persistir."""
    found = decide(outcome("review", **extra), previous_failures=FAILURES_TO_INACTIVATE - 1)
    assert (found.action, found.category, found.failures) == (
        "review",
        "access_error",
        FAILURES_TO_INACTIVATE,
    )


def test_a_failure_that_keeps_passing_for_days_is_finally_inactivated():
    found = decide(
        outcome("review", cause="access", status=429, transient=True),
        previous_failures=TRANSIENT_FAILURES_TO_INACTIVATE - 1,
    )
    assert (found.action, found.failures) == ("inactivate", TRANSIENT_FAILURES_TO_INACTIVATE)
    assert TRANSIENT_FAILURES_TO_INACTIVATE > FAILURES_TO_INACTIVATE


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (429, None, True),
        (503, None, True),
        (None, "The read operation timed out", True),
        (None, "Timeout: Failed to perform, curl: (28) Operation timed out after 25001 ms", True),
        (None, "curl: (28) Connection timed out", True),
        (403, None, False),
        (404, None, False),
        (None, "SSL: certificate verify failed", False),
    ],
)
def test_timeouts_of_every_transport_are_failures_that_pass(status, error, expected):
    """O curl escreve ``Timeout: Failed to perform``; só ``timed out`` deixava 47 links de fora."""
    assert _transient(status, error) is expected


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
    ("inactivated_days_ago", "checked_days_ago", "expected"),
    [
        (1, 1, True),  # a saúde o inativou ontem: volta a ser testado a cada rodada
        (6.9, 1, True),
        (30, 1, False),  # inativado há semanas e testado ontem: não repete todo dia
        (None, 1, False),  # nunca inativado pela saúde (inativação manual) e testado ontem
        (None, 6.9, False),
        (None, 7.1, True),  # varredura semanal de todos os inativos
        (30, None, True),  # nunca verificado pela saúde
        (None, None, True),
    ],
)
def test_recovery_retests_recent_health_inactivations_every_round_and_the_rest_weekly(
    inactivated_days_ago, checked_days_ago, expected
):
    inactivated = (
        None if inactivated_days_ago is None else NOW - timedelta(days=inactivated_days_ago)
    )
    checked = None if checked_days_ago is None else NOW - timedelta(days=checked_days_ago)
    assert recovery_due(inactivated, checked, NOW) is expected
