import importlib.util
import json
from pathlib import Path

from buscador_midias.net.fetcher import FetchResult

spec = importlib.util.spec_from_file_location(
    "http_transport_pilot", Path(__file__).resolve().parents[2] / "scripts/pilot_http_transports.py"
)
assert spec is not None and spec.loader is not None
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def test_http_200_challenge_is_not_a_valid_feed_or_article():
    response = FetchResult(
        "https://example.com/feed",
        200,
        b"<html><title>Are you a robot?</title><body>Access denied</body></html>",
        None,
        content_type="text/html",
    )
    assert not pilot.inspect_content(dict(kind="xml"), response)["xml_valid"]
    assert not pilot.inspect_content(dict(kind="article"), response)["text"]


def test_xml_success_does_not_claim_article_text_without_an_article_request():
    response = FetchResult(
        "https://example.com/feed",
        200,
        b'<rss version="2.0"><channel><item><link>https://example.com/news</link></item></channel></rss>',
        None,
        content_type="application/rss+xml",
    )
    result = pilot.inspect_content(dict(kind="xml"), response)
    assert result["xml_valid"] and result["entries"] == 1
    assert not result["text"]


def test_access_failure_does_not_confirm_text_even_with_article_like_body():
    response = FetchResult(
        "https://example.com/news",
        403,
        b"<html><article><h1>Noticia</h1><p>texto</p></article></html>",
        None,
    )
    result = pilot.inspect_content(dict(kind="article"), response)
    assert not result["accessed"]
    assert not result["text"]


def test_textual_article_and_podcast_are_distinguished():
    paragraph = "A reportagem ouviu autoridades e analisou documentos sobre as medidas anunciadas. "
    for schema, expected in [("NewsArticle", True), ("PodcastEpisode", False)]:
        structured = json.dumps(dict(**{"@type": schema}, headline="Reportagem na cidade"))
        body = (
            "<html><head><title>Reportagem na cidade</title>"
            f'<script type="application/ld+json">{structured}</script></head>'
            "<body><article><h1>Reportagem na cidade</h1>"
            + "".join(f"<p>{paragraph}</p>" for _ in range(25))
            + "</article></body></html>"
        )
        response = FetchResult(
            "https://example.com/news", 200, body.encode(), None, content_type="text/html"
        )
        result = pilot.inspect_content(dict(kind="article"), response)
        assert result["text"] is expected
