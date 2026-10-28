"""Período coberto pelo endereço de um link: fatia de dia ou mês que já fechou é histórica."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from buscador_midias.catalog.link_period import is_historical, url_period

# 27/10/2026 12h UTC = 27/10 em Brasília.
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "url, start, end",
    [
        (
            "https://oglobo.globo.com/sitemap/oglobo/2026/10/19_1.xml",
            (2026, 10, 19),
            (2026, 10, 19),
        ),
        (
            "https://x.com/arc/outboundfeeds/sitemap/2026-10-16/?outputType=xml",
            (2026, 10, 16),
            (2026, 10, 16),
        ),
        ("https://x.com/tncms/sitemap/editorial.xml?date=2026-05-28", (2026, 5, 28), (2026, 5, 28)),
        ("https://x.com/sitemap/html/hs/sitemap-2026_10_03.xml", (2026, 10, 3), (2026, 10, 3)),
        ("https://x.com/sitemap.xml?yyyy=2026&mm=09&dd=3", (2026, 9, 3), (2026, 9, 3)),
        ("https://x.com/sitemap-2026-09.xml.gz", (2026, 9, 1), (2026, 9, 30)),
        ("https://x.com/sitemap/2026/02/feeds.xml", (2026, 2, 1), (2026, 2, 28)),
        ("https://x.com/sitemap.xml?year=2026&month=12", (2026, 12, 1), (2026, 12, 31)),
        ("https://x.com/Sitemap_Archiv_2026_10.xml.gz", (2026, 10, 1), (2026, 10, 31)),
        ("https://x.com/sitemaps/sitemap.202605.xml", (2026, 5, 1), (2026, 5, 31)),
        (
            "https://x.com/sitemap-articles-from2026-10-27T00:00:00Z-to2026-10-28T00:00:00Z.xml",
            (2026, 10, 27),
            (2026, 10, 28),
        ),
    ],
)
def test_the_period_is_read_from_the_address(url, start, end):
    period = url_period(url)
    assert (period.start, period.end) == (date(*start), date(*end))


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/sitemap.xml",
        "https://x.com/post-sitemap4.xml",
        "https://x.com/sitemap-192703.xml",
        "https://x.com/news-sitemap-latest.xml",
        "https://x.com/rss/12345678.xml",
        "https://x.com/sitemap/9999/99/feeds.xml",
        "https://x.com/feed/?id=20261301",
    ],
)
def test_an_address_without_a_date_has_no_period(url):
    assert url_period(url) is None


def test_a_closed_day_is_historical():
    assert is_historical("https://o.com/sitemap/2026/10/19_1.xml", "sitemap", NOW)


def test_yesterday_and_the_overlap_margin_are_still_live():
    assert not is_historical("https://o.com/sitemap/2026/10/26.xml", "sitemap", NOW)
    assert not is_historical("https://o.com/sitemap/2026/10/25.xml", "sitemap", NOW)
    assert is_historical("https://o.com/sitemap/2026/10/24.xml", "sitemap", NOW)


def test_the_current_month_is_live_and_a_closed_month_is_historical():
    assert not is_historical("https://o.com/sitemap-2026-10.xml", "sitemap", NOW)
    assert is_historical("https://o.com/sitemap-2026-09.xml", "sitemap", NOW)


def test_the_month_just_ended_stays_live_inside_the_overlap_margin():
    early = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    assert not is_historical("https://o.com/sitemap-2026-10.xml", "sitemap", early)
    assert is_historical(
        "https://o.com/sitemap-2026-10.xml", "sitemap", datetime(2026, 11, 3, 12, tzinfo=UTC)
    )


def test_a_future_period_is_never_historical():
    assert not is_historical("https://o.com/sitemap-2026-12.xml", "sitemap", NOW)


def test_a_range_counts_by_its_latest_end():
    live = "https://x.com/s-from2026-10-27T00:00:00Z-to2026-10-28T00:00:00Z.xml"
    assert not is_historical(live, "sitemap", NOW)
    assert is_historical("https://x.com/s-from2026-09-01-to2026-09-02.xml", "sitemap", NOW)


def test_the_g1_daily_model_is_dated_on_purpose_and_never_historical():
    assert not is_historical("https://g1.globo.com/sitemap/g1/2026/09/01.xml", "g1_daily", NOW)


def test_the_day_is_taken_in_brasilia_not_utc():
    # 28/10 01h UTC ainda é 27/10 22h em Brasília.
    late = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
    assert not is_historical("https://o.com/sitemap/2026/10/25.xml", "sitemap", late)
