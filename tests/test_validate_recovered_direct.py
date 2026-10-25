"""Validação de recuperados: texto é obrigatório e rede não usa robots nem Bright."""

import gzip
import hashlib

import pytest
from test_catalog_testing import ARTICLE, ROOT, SOURCE, article, feed

from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.net.fetcher import FetchResult
from scripts import validate_recovered_direct as subject


def item(tmp_path):
    body = feed()
    path = tmp_path / "root.gz"
    path.write_bytes(gzip.compress(body))
    return dict(
        cached=dict(
            link_id=1,
            source_id=1,
            url=ROOT + "feed",
            status=200,
            final_url=ROOT + "feed",
            body_file=str(path),
            body_sha256=hashlib.sha256(body).hexdigest(),
            checked_at="2026-10-24",
        ),
        source=SOURCE,
        config=dict(
            url=ROOT + "feed",
            model="rss",
            kind="feed",
            test_id="proof",
            model_version=1,
            parameters=get_mechanism("rss").defaults(),
        ),
    )


@pytest.mark.parametrize(
    "content,confirmed",
    [
        (article(), True),
        (article("PodcastEpisode"), False),
        (b"<html>Sem reportagem</html>", False),
    ],
)
def test_cached_xml_requires_accessible_text(tmp_path, monkeypatch, content, confirmed):
    (tmp_path / "bodies").mkdir()
    calls = []

    def get(reader, url):
        calls.append(url)
        reader.requests += 1
        assert reader._allowed(url) == (True, "")
        return FetchResult(url, 200, content, None, url, "text/html")

    monkeypatch.setattr(subject.TestReader, "get", get)
    row = subject.validate(item(tmp_path), tmp_path)
    assert row["result"]["usable_for_news"] is confirmed
    assert calls == [ARTICLE]
    assert row["result"]["bright_data_requests"] == 0
    assert row["result"]["robots_requests"] == 0
    assert row["result"]["root_cached"]


def test_bad_root_hash_never_calls_network(tmp_path, monkeypatch):
    record = item(tmp_path)
    record["cached"]["body_sha256"] = "wrong"
    monkeypatch.setattr(subject.TestReader, "get", lambda *args: pytest.fail("Não deve acessar"))
    result = subject.validate(record, tmp_path)["result"]
    assert not result["usable_for_news"]
    assert "adulterado" in result["error"]


@pytest.mark.parametrize("status,attempts", [(503, 3), (403, 1), (200, 1)])
def test_limited_http_retries(tmp_path, monkeypatch, status, attempts):
    (tmp_path / "bodies").mkdir()
    calls = []

    def get(reader, url):
        reader._scope(url)
        calls.append(url)
        return FetchResult(url, status, b"", None, url)

    monkeypatch.setattr(subject.TestReader, "get", get)
    reader = subject.ValidationReader([ROOT], lambda _: None, {"url": ROOT + "feed"}, b"", tmp_path)
    reader.get(ARTICLE)
    assert len(calls) == attempts
