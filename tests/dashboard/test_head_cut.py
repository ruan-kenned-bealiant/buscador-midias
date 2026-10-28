"""Corte pelo topo: com ordem ``newest_first`` comprovada, topo fora da janela dispensa o resto."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.catalog.head_probe import prefix_outside
from buscador_midias.ingestion.flow import head_outside

SINCE = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)


def stamp(delta: timedelta) -> str:
    return (SINCE + delta).isoformat()


def test_top_before_the_window_cuts_when_the_order_is_proven():
    found = head_outside("newest_first", stamp(timedelta(days=-3)), SINCE)
    assert found and found["rule"] == "head_before_window"


@pytest.mark.parametrize("order", ["unknown", "unordered", "oldest_first"])
def test_without_a_proven_newest_first_order_nothing_is_cut(order):
    assert head_outside(order, stamp(timedelta(days=-30)), SINCE) is None


@pytest.mark.parametrize(
    "top", [None, "", "lixo", stamp(timedelta(hours=1)), stamp(timedelta(hours=-6)), "2026-10-25"]
)
def test_a_top_that_may_belong_to_the_window_is_never_cut(top):
    assert head_outside("newest_first", top, SINCE) is None


def rss(days_ago, *, n=30, truncated=False):
    items = "".join(
        f"<item><title>t{i}</title><link>https://a.example/n{i}</link>"
        f"<pubDate>{(SINCE - timedelta(days=days_ago + i)).strftime('%a, %d %b %Y %H:%M:%S +0000')}"
        "</pubDate></item>"
        for i in range(n)
    )
    body = f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{items}'
    return (body[: len(body) // 2] if truncated else body + "</channel></rss>").encode()


def test_a_truncated_feed_whose_top_is_old_is_cut():
    assert prefix_outside(rss(5, truncated=True), SINCE) is not None


def test_a_truncated_feed_with_news_in_the_window_is_not_cut():
    assert prefix_outside(rss(-1, truncated=True), SINCE) is None


def test_a_complete_document_is_handled_the_same_way():
    assert prefix_outside(rss(5), SINCE) is not None
    assert prefix_outside(rss(-1), SINCE) is None


def test_few_dated_entries_in_the_prefix_are_not_enough_to_cut():
    assert prefix_outside(rss(5, n=2), SINCE) is None


def test_a_prefix_that_breaks_the_descending_order_is_never_cut():
    # O topo é antigo, mas o sexto item é de hoje: a ordem prometida não vale mais.
    ages = [9, 10, 11, 12, 13, -1, 15, 16, 17, 18]
    items = "".join(
        f"<item><link>https://a.example/n{i}</link><pubDate>"
        f"{(SINCE - timedelta(days=d)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for i, d in enumerate(ages)
    )
    body = f'<rss version="2.0"><channel>{items}'.encode()
    assert prefix_outside(body, SINCE) is None


def urlset(days_ago, *, n=30):
    urls = "".join(
        f"<url><loc>https://a.example/n{i}</loc>"
        f"<lastmod>{(SINCE - timedelta(days=days_ago + i)).isoformat()}</lastmod></url>"
        for i in range(n)
    )
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}'.encode()


def test_a_truncated_sitemap_uses_lastmod_as_the_top():
    assert prefix_outside(urlset(5), SINCE) is not None
    assert prefix_outside(urlset(-1), SINCE) is None


def test_a_sitemap_index_is_cut_by_the_lastmod_of_its_first_child():
    children = "".join(
        f"<sitemap><loc>https://a.example/s{i}.xml</loc>"
        f"<lastmod>{(SINCE - timedelta(days=9 + i)).isoformat()}</lastmod></sitemap>"
        for i in range(30)
    )
    assert prefix_outside(f"<sitemapindex>{children}".encode(), SINCE) is not None


@pytest.mark.parametrize("body", [b"", b"<html><body>erro 503</body></html>", b"\x1f\x8b\x08lixo"])
def test_anything_that_is_not_a_readable_feed_or_sitemap_is_never_cut(body):
    assert prefix_outside(body, SINCE) is None
