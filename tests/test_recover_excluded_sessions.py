"""Rodada dos excluídos: limites, pausas e comprovação textual antes de restaurar."""

import gzip
import hashlib
import time
from pathlib import Path

import pytest
from test_catalog_testing import ARTICLE, ROOT, SOURCE, article, feed

from buscador_midias.catalog.batch_reader import BatchCache
from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.net.fetcher import FetchResult
from scripts import recover_excluded_sessions as subject


def item():
    return dict(
        source=SOURCE,
        config=dict(
            url=ROOT + "feed",
            kind="feed",
            model="rss",
            model_version=1,
            parameters=get_mechanism("rss").defaults(),
            test_id="one",
            bright_data=False,
        ),
    )


@pytest.mark.parametrize(
    "status,updated,final",
    [(403, False, True), (403, True, False), (503, False, False), (404, False, True)],
)
def test_root_probes_are_single_attempt_and_classify_permanent_errors(
    tmp_path, monkeypatch, status, updated, final
):
    (tmp_path / "bodies").mkdir()

    def get(reader, url):
        reader.requests += 1
        reader.session_updated = updated
        return FetchResult(url, status, b"", None, url)

    monkeypatch.setattr(subject.TestReader, "get", get)
    result = subject.root_probe(item(), tmp_path, {})
    assert result["physical_requests"] == 1 and result["attempt"] == 1
    assert result["final"] is final
    assert result["xml_valid"] is False


def test_third_failure_is_final_and_preserves_request_count(tmp_path, monkeypatch):
    def get(reader, url):
        reader.requests += 1
        return FetchResult(url, 503, b"", None, url)

    monkeypatch.setattr(subject.TestReader, "get", get)
    result = subject.root_probe(item(), tmp_path, dict(attempt=2, physical_requests=2))
    assert result["attempt"] == 3 and result["physical_requests"] == 3 and result["final"]


def test_retry_after_schedules_future_host_without_sleeping_worker(tmp_path, monkeypatch):
    def get(reader, url):
        reader.requests += 1
        reader.retry_after_seconds = 120
        reader.retry_after_host = "news.example"
        return FetchResult(url, 429, b"", None, url, retry_after_seconds=120)

    monkeypatch.setattr(subject.TestReader, "get", get)
    result = subject.root_probe(item(), tmp_path, {})
    assert not result["final"]
    assert result["retry_at"] >= time.time() + 119
    assert result["retry_host"] == "news.example"


@pytest.mark.parametrize(
    "body,confirmed",
    [(article(), True), (article("PodcastEpisode"), False), (b"<html>Sem texto</html>", False)],
)
def test_xml_is_reused_and_old_text_can_confirm_but_audio_cannot(
    tmp_path, monkeypatch, body, confirmed
):
    (tmp_path / "bodies").mkdir()
    xml = feed()
    path = tmp_path / "feed.gz"
    path.write_bytes(gzip.compress(xml))
    cached = dict(
        url=ROOT + "feed",
        xml_valid=True,
        kind="feed",
        entries=1,
        children=0,
        status=200,
        final_url=ROOT + "feed",
        body_file=str(path),
        body_sha256=hashlib.sha256(xml).hexdigest(),
    )
    calls = []

    def get(reader, url):
        calls.append(url)
        reader.requests += 1
        return FetchResult(url, 200, body, None, url, "text/html")

    monkeypatch.setattr(subject.TestReader, "get", get)
    result = subject.validate(item(), cached, tmp_path, time.time() + 60, BatchCache())
    assert result["usable_for_news"] is confirmed
    assert calls == [ARTICLE]
    assert result["bright_data_requests"] == 0 and result["robots_requests"] == 0
    observation = result["observations"][0]
    assert gzip.decompress(Path(observation["body_file"]).read_bytes()) == body


def test_wait_longer_than_round_is_deferred_without_http(tmp_path, monkeypatch):
    manifest = dict(
        created_at=subject.datetime.now(subject.UTC).isoformat(),
        root_deadline_seconds=30,
        links=[item()],
    )
    future = dict(
        url=ROOT + "feed",
        final=False,
        retry_at=time.time() + 120,
        attempt=1,
        retry_host="news.example",
        server_until=time.time() + 120,
    )
    subject.append(tmp_path / "raizes.jsonl", future)
    monkeypatch.setattr(
        subject, "root_probe", lambda *args: pytest.fail("Não deve acessar antes do prazo")
    )
    states = subject.roots_phase(manifest, tmp_path, lambda *args: None)
    assert states[ROOT + "feed"]["deferred"]
    assert states[ROOT + "feed"]["error"] == "server_wait_exceeds_budget"
