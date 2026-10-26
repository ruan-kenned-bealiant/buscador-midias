from datetime import UTC, datetime

import pytest

from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.discovery.feeds import parse_feed_entries
from buscador_midias.domain.site import Site

FEED = "https://feeds.folha.uol.com.br/poder/rss091.xml"
ARTICLE = "https://www1.folha.uol.com.br/poder/2026/10/noticia.shtml?origem=rss"
WRAPPER = "https://redir.folha.com.br/redir/online/poder/rss091/*"


def xml(url):
    return (
        '<rss version="2.0"><channel><item><title>Notícia</title>'
        f"<link>{url.replace('&', '&amp;')}</link>"
        "<pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"
    ).encode()


def test_official_folha_redirect_preserves_destination_and_publication():
    entries = parse_feed_entries(xml(WRAPPER + ARTICLE), FEED)
    assert entries[0].url == ARTICLE
    assert entries[0].published == datetime(2026, 10, 26, 10, tzinfo=UTC)
    document = parse_document(xml(WRAPPER + ARTICLE), FEED)
    assert document["entries"] == [ARTICLE]
    assert ARTICLE in document["dates"]
    assert ARTICLE in document["hints"]
    site = Site.create(
        name="Folha",
        url="https://www.folha.uol.com.br",
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )
    assert site.owns(entries[0].url)


@pytest.mark.parametrize(
    "target",
    [
        "https://folha.uol.com.br.evil.example/noticia",
        "http://127.0.0.1/noticia",
        "https://other.example/noticia",
        "https://user@www1.folha.uol.com.br/noticia",
        "https://www1.folha.uol.com.br:8080/noticia",
        "javascript:alert(1)",
    ],
)
def test_does_not_unwrap_unrelated_or_unsafe_targets(target):
    original = WRAPPER + target
    assert parse_feed_entries(xml(original), FEED)[0].url == original


def test_does_not_rewrite_other_publishers_or_arbitrary_redirectors():
    original = WRAPPER + ARTICLE
    assert parse_feed_entries(xml(original), "https://evil.example/feed")[0].url == original
    other = original.replace("redir.folha.com.br", "redir.folha.com.br.evil.example")
    assert parse_feed_entries(xml(other), FEED)[0].url == other
    assert parse_feed_entries(xml(ARTICLE), FEED)[0].url == ARTICLE
