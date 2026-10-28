"""Leitura parcial: o leitor entrega o prefixo ao chamador, que pode dispensar o resto."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from buscador_midias.catalog import curl_reader
from buscador_midias.catalog.curl_reader import CurlReader
from buscador_midias.catalog.http_session import HttpSessions
from buscador_midias.catalog.reader import EARLY_STOP_BYTES, EARLY_STOP_STEPS, TestReader

URL = "https://news.example/feed.xml"
CHUNK = b"x" * (16 * 1024)


class Recorder:
    """Chamador de ``early_stop``: guarda o que viu e responde o combinado."""

    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def __call__(self, prefix: bytes) -> bool:
        self.seen.append(len(prefix))
        return self.answer


@pytest.fixture
def direct(monkeypatch):
    monkeypatch.setattr("buscador_midias.catalog.reader.public_ip", lambda *args: "93.184.216.34")
    state = SimpleNamespace(reads=0, chunks=0, closed=False)

    class Response:
        status = 200

        def getheader(self, name, default=None):
            return "application/xml" if name == "Content-Type" else default

        def read1(self, size):
            if state.reads >= state.chunks:
                return b""
            state.reads += 1
            return CHUNK

    class Connection:
        def __init__(self, *args):
            self.sock = None

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            state.closed = True

    monkeypatch.setattr("buscador_midias.catalog.reader._PinnedConnection", Connection)
    return state


def test_a_true_answer_stops_reading_and_returns_the_prefix(direct):
    direct.chunks = 100
    stop = Recorder(True)
    result = TestReader(["https://news.example"], lambda _: None).get(URL, early_stop=stop)
    assert result.truncated and result.status == 200 and result.error is None
    assert len(result.content) == direct.reads * len(CHUNK)
    assert direct.reads * len(CHUNK) >= EARLY_STOP_BYTES
    assert direct.reads < 100
    assert stop.seen == [len(result.content)]


def test_a_false_answer_reads_everything_and_asks_again_only_at_larger_prefixes(direct):
    direct.chunks = 10  # 160 KiB: cruza 32 KiB e 128 KiB
    stop = Recorder(False)
    result = TestReader(["https://news.example"], lambda _: None).get(URL, early_stop=stop)
    assert not result.truncated
    assert len(result.content) == 10 * len(CHUNK)
    assert len(stop.seen) == 2 and stop.seen[0] >= EARLY_STOP_STEPS[0]
    assert stop.seen[1] >= EARLY_STOP_STEPS[1] > stop.seen[0]


def test_a_large_document_is_asked_at_every_step_until_the_answer_is_true(direct):
    direct.chunks = 200  # 3,2 MiB
    answers = iter([False, False, True])
    seen = []

    def stop(prefix):
        seen.append(len(prefix))
        return next(answers)

    result = TestReader(["https://news.example"], lambda _: None).get(URL, early_stop=stop)
    assert result.truncated and len(seen) == 3
    assert seen[2] >= EARLY_STOP_STEPS[2] and len(result.content) == seen[2]


def test_a_small_document_never_asks(direct):
    direct.chunks = 1
    stop = Recorder(True)
    result = TestReader(["https://news.example"], lambda _: None).get(URL, early_stop=stop)
    assert not result.truncated and stop.seen == []


def test_without_early_stop_behaviour_is_unchanged(direct):
    direct.chunks = 5
    result = TestReader(["https://news.example"], lambda _: None).get(URL)
    assert not result.truncated and len(result.content) == 5 * len(CHUNK)


@pytest.fixture
def curl(monkeypatch):
    from curl_cffi import requests

    state = SimpleNamespace(chunks=10, delivered=0, raises=True)

    class Session:
        def __init__(self, **kwargs):
            self.curl_options = {}

        def get(self, url, **kwargs):
            for _ in range(state.chunks):
                state.delivered += 1
                refused = kwargs["content_callback"](CHUNK) == 0
                if refused and state.raises:
                    raise RuntimeError("Failed writing body (23)")
            return SimpleNamespace(
                status_code=200, headers={"Content-Type": "text/xml"}, http_version=3
            )

        def close(self):
            pass

    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(curl_reader, "public_ip", lambda *_: "8.8.8.8")
    curl_reader._IPS.clear()
    return state


def curl_get(stop):
    reader = CurlReader(
        ["https://news.example"],
        lambda _: None,
        transport="curl_chrome_h2",
        seconds=10,
        requests=4,
        sessions=HttpSessions(),
    )
    return reader.get(URL, early_stop=stop)


def test_curl_stops_the_transfer_when_asked(curl):
    curl.chunks = 100
    stop = Recorder(True)
    result = curl_get(stop)
    assert result.truncated and result.status == 200 and result.error is None
    assert result.content and len(result.content) >= EARLY_STOP_BYTES
    assert curl.delivered * len(CHUNK) >= EARLY_STOP_BYTES
    assert curl.delivered < 100
    assert len(stop.seen) == 1


def test_curl_that_keeps_delivering_after_the_stop_does_not_grow_the_body(curl):
    # Defeito real visto ao vivo: o transporte seguia entregando após o primeiro retorno 0.
    curl.chunks, curl.raises = 100, False
    result = curl_get(Recorder(True))
    assert result.truncated and result.status == 200
    assert len(result.content) < 100 * len(CHUNK)
    assert len(result.content) < 2 * EARLY_STOP_BYTES


def test_curl_reads_everything_when_the_answer_is_false(curl):
    stop = Recorder(False)
    result = curl_get(stop)
    assert not result.truncated and len(result.content) == 10 * len(CHUNK)
    assert len(stop.seen) == 2
