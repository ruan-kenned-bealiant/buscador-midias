"""Cache HTTP limitado sem misturar transporte, fonte ou validadores."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from buscador_midias.ingestion.document_memo import DocumentMemo
from buscador_midias.net.fetcher import FetchResult


def response(status=200):
    return FetchResult("https://example.test/feed", status, b"<rss/>", None)


def test_identical_concurrent_requests_share_http_but_not_request_keys():
    memo = DocumentMemo()
    started, release = Event(), Event()
    calls = []

    def fetch():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return response()

    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(memo.get, ("run", "source", "transport", "url", "etag"), fetch)
        assert started.wait(2)
        second = executor.submit(memo.get, ("run", "source", "transport", "url", "etag"), fetch)
        release.set()
        assert first.result() == (response(), False)
        assert second.result() == (response(), True)
    assert len(calls) == 1
    for key in [("run2",), ("source2",), ("transport2",), ("etag2",)]:
        assert memo.get(key, lambda: response())[1] is False


@pytest.mark.parametrize("status", [304, 403, 503])
def test_unsuccessful_responses_are_not_shared(status):
    memo = DocumentMemo()
    assert memo.get(("key",), lambda: response(status))[1] is False
    assert memo.get(("key",), lambda: response(status))[1] is False


def test_expired_and_oversized_entries_are_not_reused(monkeypatch):
    memo = DocumentMemo(ttl=5)
    monkeypatch.setattr("buscador_midias.ingestion.document_memo.time.monotonic", lambda: 10)
    memo.get(("a",), lambda: response())
    monkeypatch.setattr("buscador_midias.ingestion.document_memo.time.monotonic", lambda: 16)
    assert memo.get(("a",), lambda: response())[1] is False
    tiny = DocumentMemo(max_bytes=1)
    tiny.get(("a",), lambda: response())
    assert tiny.size == 0
    assert tiny.get(("a",), lambda: response())[1] is False


def test_fetch_exception_releases_reservation():
    memo = DocumentMemo()

    def fail():
        raise ValueError("interrupted")

    with pytest.raises(ValueError):
        memo.get(("a",), fail)
    assert not memo.inflight
    assert memo.get(("a",), lambda: response()) == (response(), False)


def test_entry_count_is_bounded_and_eviction_keeps_accounting():
    memo = DocumentMemo()
    for i in range(129):
        memo.get((str(i),), lambda: response())
    assert len(memo.entries) == 128
    assert memo.size == sum(len(entry[2]) for entry in memo.entries.values())
    assert memo.get(("0",), lambda: response())[1] is False
