"""Reteste HTTP isolado: sem rede, corpus ou serviços pagos."""

import gzip

import pytest

from buscador_midias.catalog.store import CatalogError
from buscador_midias.net.fetcher import FetchResult
from scripts import retest_excluded_direct as subject

URL = "https://example.com/feed"


def test_url_budget_and_redirect_cycle_preserve_scope():
    counts = {}
    for _ in range(3):
        reader = subject.DirectProbeReader(URL, counts, 2)
        assert reader._scope(URL) == URL
        assert reader._allowed(URL) == (True, "")
    with pytest.raises(CatalogError, match="request_budget"):
        subject.DirectProbeReader(URL, counts, 2)._scope(URL)
    reader = subject.DirectProbeReader(URL, {}, 2)
    reader._scope(URL)
    with pytest.raises(CatalogError, match="redirect_cycle"):
        reader._scope(URL)
    with pytest.raises(CatalogError, match="fora dos domínios"):
        subject.DirectProbeReader(URL, {}, 2)._scope("https://other.example/feed")


@pytest.mark.parametrize("status,expected", [(403, 1), (429, 1), (503, 3)])
def test_retry_is_bounded_and_does_not_repeat_permanent_errors(
    tmp_path, monkeypatch, status, expected
):
    class Reader:
        requests = 1

        def __init__(self, *args):
            pass

        def get(self, url):
            return FetchResult(url, status, b"", None)

    monkeypatch.setattr(subject, "DirectProbeReader", Reader)
    monkeypatch.setattr(subject.time, "sleep", lambda _: None)
    result = subject.probe(URL, tmp_path, 2)
    assert result["attempt_count"] == expected
    assert result["http_requests"] == expected
    assert not result["http_ok"]
    assert not result["xml_valid"]
    assert result["robots_requests"] == result["bright_data_requests"] == 0


def test_success_parses_xml_and_preserves_received_bytes(tmp_path, monkeypatch):
    body = b'<rss version="2.0"><channel><title>News</title><item><title>Story</title><link>https://example.com/story</link></item></channel></rss>'

    class Reader:
        requests = 1

        def __init__(self, *args):
            pass

        def get(self, url):
            return FetchResult(url, 200, body, None)

    (tmp_path / "bodies").mkdir()
    monkeypatch.setattr(subject, "DirectProbeReader", Reader)
    result = subject.probe(URL, tmp_path, 2)
    assert result["attempt_count"] == 1
    assert result["http_ok"] and result["xml_valid"]
    assert result["entries"] == 1
    assert result["category"] == "xml_with_urls"
    assert gzip.decompress(next((tmp_path / "bodies").glob("*.gz")).read_bytes()) == body
