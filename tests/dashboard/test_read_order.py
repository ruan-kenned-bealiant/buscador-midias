"""Ordem em que um documento lista suas entradas: de onde vem o corte seguro."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.catalog.read_order import (
    NEWEST_FIRST,
    OLDEST_FIRST,
    UNKNOWN,
    UNORDERED,
    detect_read_order,
)

BASE = datetime(2026, 10, 1, 12, 7, tzinfo=UTC)  # nunca cai na meia-noite exata
MIDNIGHT = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)  # data só de dia


def document(hours: list[int | None], *, key="dates", index=False):
    """Documento com uma entrada por posição; ``None`` fica sem data."""
    urls = [f"https://a.example/n{n}" for n in range(len(hours))]
    stamps = {
        u: (BASE + timedelta(hours=h)).isoformat()
        for u, h in zip(urls, hours, strict=True)
        if h is not None
    }
    base = dict(entries=[], dates={}, hints={}, children=[], child_dates={}, index=index)
    if index:
        base.update(children=urls, child_dates=stamps)
    else:
        base.update(entries=urls, **{key: stamps})
    return base


def test_newest_on_top_is_newest_first():
    assert detect_read_order(document(list(range(40, 0, -1)))) == NEWEST_FIRST


def test_newest_at_the_bottom_is_oldest_first():
    assert detect_read_order(document(list(range(1, 41)))) == OLDEST_FIRST


def test_a_shuffled_document_is_unordered():
    assert detect_read_order(document([5, 30, 2, 40, 7, 22, 1, 35, 9, 18])) == UNORDERED


def test_a_pinned_old_item_on_top_is_not_newest_first():
    hours = [-500, *range(40, 0, -1)]
    assert detect_read_order(document(hours)) == UNORDERED


def test_equal_dates_do_not_break_the_order():
    hours = [40, 40, 39, 39, 39, 30, 30, 20, 20, 10]
    assert detect_read_order(document(hours)) == NEWEST_FIRST


def test_all_dates_equal_say_nothing_about_order():
    assert detect_read_order(document([5] * 12)) == UNKNOWN


def test_too_few_dated_entries_is_unknown():
    assert detect_read_order(document([9, 8, 7, 6])) == UNKNOWN


def test_mostly_undated_is_unknown():
    hours = [9, None, None, 8, None, None, 7, None, None, 6, None, None]
    assert detect_read_order(document(hours)) == UNKNOWN


def test_a_single_undated_entry_makes_the_order_unprovable():
    # O corte descartaria a entrada sem data junto com o resto: só vale com tudo datado.
    hours = [*range(40, 30, -1), None]
    assert detect_read_order(document(hours)) == UNKNOWN


def day_only(days_ago: list[int]):
    """Documento cujas datas são só o dia (meia-noite exata), como muitos ``lastmod``."""
    urls = [f"https://a.example/n{n}" for n in range(len(days_ago))]
    stamps = {
        u: (MIDNIGHT - timedelta(days=d)).isoformat() for u, d in zip(urls, days_ago, strict=True)
    }
    return dict(entries=urls, dates={}, hints=stamps, children=[], child_dates={}, index=False)


def test_a_document_with_only_day_precision_keeps_a_provable_order():
    assert detect_read_order(day_only(list(range(0, 12)))) == NEWEST_FIRST


def test_a_day_only_entry_that_may_end_after_the_top_makes_the_order_unprovable():
    # O dia inteiro de uma data só-dia pode acabar depois do horário da entrada do topo.
    doc = day_only(list(range(0, 12)))  # a primeira é do mesmo dia do topo
    top = "https://a.example/top"
    doc["entries"].insert(0, top)
    doc["hints"][top] = (MIDNIGHT + timedelta(hours=14)).isoformat()
    assert detect_read_order(doc) == UNKNOWN


def test_an_old_midnight_timestamp_below_a_later_top_does_not_spoil_the_order():
    doc = day_only(list(range(3, 12)))
    top = "https://a.example/top"
    doc["entries"].insert(0, top)
    doc["hints"][top] = (MIDNIGHT + timedelta(hours=14)).isoformat()
    assert detect_read_order(doc) == NEWEST_FIRST


def test_update_hints_are_used_when_there_is_no_publication_date():
    assert detect_read_order(document(list(range(40, 0, -1)), key="hints")) == NEWEST_FIRST


def test_an_index_is_read_by_the_lastmod_of_its_children():
    assert detect_read_order(document(list(range(40, 0, -1)), index=True)) == NEWEST_FIRST


def test_sub_minute_jitter_is_tolerated_but_real_inversions_are_not():
    urls = [f"https://a.example/n{n}" for n in range(10)]
    stamps = {
        u: (BASE - timedelta(hours=n, seconds=(30 if n % 2 else 0))).isoformat()
        for n, u in enumerate(urls)
    }
    jitter = dict(entries=urls, dates=stamps, hints={}, children=[], child_dates={}, index=False)
    assert detect_read_order(jitter) == NEWEST_FIRST
    stamps[urls[5]] = (BASE + timedelta(hours=3)).isoformat()
    assert detect_read_order(jitter) == UNORDERED


@pytest.mark.parametrize("bad", ["", "lixo", None, 0])
def test_an_unreadable_date_counts_as_undated_and_blocks_the_order(bad):
    doc = document(list(range(40, 0, -1)))
    doc["dates"]["https://a.example/n3"] = bad
    assert detect_read_order(doc) == UNKNOWN
