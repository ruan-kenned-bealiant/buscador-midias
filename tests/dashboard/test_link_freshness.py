"""Frescor de um link: a data mais nova que o próprio feed ou sitemap declara."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.catalog.freshness import (
    DELETE_AFTER,
    INACTIVATE_AFTER,
    document_freshness,
    link_verdict,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def document(**changes):
    base = dict(
        kind="feed",
        entries=[],
        children=[],
        index=False,
        news=False,
        dates={},
        child_dates={},
        hints={},
    )
    base.update(changes)
    return base


def days(count: int) -> str:
    return (NOW - timedelta(days=count)).isoformat()


def test_publication_dates_win_and_are_exact():
    found = document_freshness(
        document(dates={"a": days(40), "b": days(3)}, hints={"a": days(1)}), NOW
    )
    assert found.newest == NOW - timedelta(days=3)
    assert found.basis == "publication"
    assert found.exact


def test_update_dates_are_only_an_upper_bound_when_there_is_no_publication_date():
    found = document_freshness(document(hints={"a": days(90), "b": days(60)}), NOW)
    assert found.newest == NOW - timedelta(days=60)
    assert found.basis == "update"
    assert not found.exact


def test_index_children_lastmod_is_the_last_resort_and_an_upper_bound():
    found = document_freshness(
        document(kind="sitemap", index=True, child_dates={"x": days(400), "y": days(500)}), NOW
    )
    assert found.newest == NOW - timedelta(days=400)
    assert found.basis == "index_lastmod"
    assert not found.exact


def test_no_date_at_all_is_unknown():
    found = document_freshness(document(entries=["https://a.example/1"]), NOW)
    assert found.newest is None
    assert found.basis == "none"


def test_dates_beyond_the_timezone_tolerance_in_the_future_are_ignored():
    found = document_freshness(
        document(dates={"a": (NOW + timedelta(days=30)).isoformat(), "b": days(5)}), NOW
    )
    assert found.newest == NOW - timedelta(days=5)


def test_a_few_hours_ahead_is_tolerated_as_local_time_without_zone():
    ahead = (NOW + timedelta(hours=3)).isoformat()
    assert document_freshness(document(dates={"a": ahead}), NOW).newest == NOW + timedelta(hours=3)


def test_unreadable_dates_are_skipped():
    found = document_freshness(document(dates={"a": "ontem", "b": days(2)}), NOW)
    assert found.newest == NOW - timedelta(days=2)


def test_naive_dates_are_read_as_utc():
    naive = (NOW - timedelta(days=4)).replace(tzinfo=None).isoformat()
    assert document_freshness(document(dates={"a": naive}), NOW).newest == NOW - timedelta(days=4)


@pytest.mark.parametrize(
    ("age", "verdict"),
    [
        (0, "keep"),
        (30, "keep"),
        (31, "inactivate"),
        (364, "inactivate"),
        (365, "delete"),
        (4000, "delete"),
    ],
)
def test_the_rule_is_one_month_to_inactivate_and_one_year_to_delete(age, verdict):
    found = document_freshness(document(dates={"a": days(age)}), NOW)
    assert link_verdict(found, NOW) == verdict


def test_limits_are_one_month_and_one_year():
    assert timedelta(days=30) == INACTIVATE_AFTER
    assert timedelta(days=365) == DELETE_AFTER


def test_without_a_date_the_verdict_is_unknown_never_a_removal():
    assert link_verdict(document_freshness(document(), NOW), NOW) == "unknown"


def test_an_upper_bound_that_is_already_old_still_proves_the_link_is_stale():
    found = document_freshness(document(hints={"a": days(500)}), NOW)
    assert link_verdict(found, NOW) == "delete"


def test_an_upper_bound_that_looks_recent_is_kept_without_claiming_freshness():
    found = document_freshness(document(hints={"a": days(2)}), NOW)
    assert link_verdict(found, NOW) == "keep"
    assert not found.exact


@pytest.mark.parametrize(
    "bogus", ["1970-01-01T00:00:00+00:00", "0001-01-01T00:00:00", "1999-12-31"]
)
def test_placeholder_dates_before_the_web_news_era_are_ignored(bogus):
    found = document_freshness(document(dates={"a": bogus}), NOW)
    assert found.newest is None
    assert link_verdict(found, NOW) == "unknown"


def test_a_placeholder_date_does_not_hide_a_real_one():
    found = document_freshness(document(dates={"a": "1970-01-01", "b": days(3)}), NOW)
    assert found.newest == NOW - timedelta(days=3)


def test_a_placeholder_publication_falls_back_to_the_next_signal():
    found = document_freshness(document(dates={"a": "1970-01-01"}, hints={"a": days(500)}), NOW)
    assert found.basis == "update" and link_verdict(found, NOW) == "delete"
