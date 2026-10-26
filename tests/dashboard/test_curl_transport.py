from types import SimpleNamespace

import pytest

from buscador_midias.catalog import curl_reader
from buscador_midias.catalog.curl_reader import CurlReader
from buscador_midias.catalog.http_session import HttpSessions
from buscador_midias.catalog.http_transport import make_reader, validate_transport
from buscador_midias.catalog.reader import TestLimitError, TestReader
from buscador_midias.net.fetcher import FetchResult


@pytest.fixture
def fake_session(monkeypatch):
    from curl_cffi import requests

    instances = []

    class Session:
        def __init__(self, **kwargs):
            self.settings = kwargs
            self.curl_options = {}
            self.calls = []
            self.responses = []
            self.closed = False
            instances.append(self)

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            response = self.responses.pop(0) if self.responses else (200, {}, b"<rss/>", 3)
            status, headers, body, version = response
            kwargs["content_callback"](body)
            return SimpleNamespace(status_code=status, headers=headers, http_version=version)

        def close(self):
            self.closed = True

    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(curl_reader, "public_ip", lambda *_: "8.8.8.8")
    curl_reader._IPS.clear()
    return instances


def reader(profile="curl_chrome_h2", **kwargs):
    return CurlReader(
        ["https://news.example"],
        lambda _: None,
        transport=profile,
        seconds=10,
        requests=4,
        sessions=HttpSessions(),
        **kwargs,
    )


def test_default_reader_is_unchanged_and_unknown_profile_is_rejected():
    assert type(make_reader(["https://news.example"], lambda _: None)) is TestReader
    with pytest.raises(ValueError):
        validate_transport("disable_tls")


def test_h2_profile_pins_public_ip_and_reports_actual_protocol(fake_session):
    from curl_cffi import CurlHttpVersion, CurlOpt

    r = reader()
    response = r.get("https://news.example/rss")
    session = fake_session[0]
    assert response.content == b"<rss/>" and response.status == 200
    assert session.settings["trust_env"] is False
    assert session.curl_options[CurlOpt.RESOLVE] == ["news.example:443:8.8.8.8"]
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["http_version"] == CurlHttpVersion.V2_PRIOR_KNOWLEDGE
    assert r.transport_events[0]["protocol"] == "h2"
    r.close()
    assert session.closed


def test_h1_bot_profile_keeps_bot_identity(fake_session):
    from curl_cffi import CurlHttpVersion

    r = reader("curl_bot_h1")
    r.get("https://news.example/rss")
    assert fake_session[0].settings["headers"]["User-Agent"] == "BuscadorMidiasBot/0.1"
    assert fake_session[0].calls[0][1]["http_version"] == CurlHttpVersion.V1_1
    r.close()


def test_private_redirect_is_rejected_before_a_second_connection(fake_session):
    r = reader()
    fake_session[0].responses = [(302, {"Location": "http://127.0.0.1/private"}, b"", 3)]
    response = r.get("https://news.example/rss")
    assert response.error and response.status is None
    assert len(fake_session[0].calls) == 1
    r.close()


def test_response_budget_and_retry_after_are_enforced(fake_session):
    r = reader(max_bytes=2)
    with pytest.raises(TestLimitError):
        r.get("https://news.example/rss")
    r.close()
    r = reader()
    fake_session[-1].responses = [(503, {"Retry-After": "60"}, b"", 3)]
    assert r.get("https://news.example/rss").retry_after_seconds == 60
    response = r.get("https://news.example/rss")
    assert response.error == "host_cooldown" and r.requests == 1
    r.close()


def test_run_test_selects_transport_without_enabling_paid_fallback(monkeypatch):
    from buscador_midias.catalog.testing import run_test

    captured = {}

    class Fake:
        requests = 0

        def get(self, url):
            self.requests += 1
            return FetchResult(url, 200, b"<rss><channel/></rss>", None, url, "application/rss+xml")

        def close(self):
            captured["closed"] = True

    def factory(roots, progress, **kwargs):
        captured.update(kwargs)
        return Fake()

    monkeypatch.setattr("buscador_midias.catalog.testing.make_reader", factory)
    result = run_test(
        dict(
            test_id="x",
            url="https://news.example/rss",
            model="rss",
            kind="feed",
            bright_data=True,
            access_transport="curl_chrome_h2",
        ),
        dict(id=1, name="News", url="https://news.example"),
        lambda _: None,
    )
    assert captured["transport"] == "curl_chrome_h2" and captured["closed"]
    assert result["access_method"] == "curl" and not result["bright_data_enabled"]
    assert result["bright_data_requests"] == 0


def test_last_permitted_request_is_sent_once(fake_session):
    """Orçamento de uma requisição permite uma leitura real e impede a segunda."""
    r = reader()
    r.max_requests = 1
    assert r.get("https://news.example/rss").status == 200
    assert r.requests == 1
    with pytest.raises(TestLimitError):
        r.get("https://news.example/rss")
    assert len(fake_session[0].calls) == 1
    r.close()
