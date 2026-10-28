"""Fallback do gerenciador preserva escopo, orçamento e status do destino."""

import json

import httpx
import pytest

from buscador_midias.catalog.bright_reader import BrightTestReader, should_fallback
from buscador_midias.catalog.health_policy import classify
from buscador_midias.catalog.reader import TestReader
from buscador_midias.net.fetcher import FetchResult

URL = "https://public.example/feed/"


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "fake-secret")
    monkeypatch.setenv("BRIGHTDATA_ZONE_UNLOCKER", "test-zone")
    monkeypatch.setattr("buscador_midias.catalog.bright_reader.public_ip", lambda *args: "8.8.8.8")


def reader(monkeypatch, handler, **extra):
    monkeypatch.setattr(
        TestReader, "_fetch", lambda self, url, **kwargs: FetchResult(url, 403, b"", None, url)
    )
    monkeypatch.setattr(TestReader, "_allowed", lambda self, url: (True, ""))
    return BrightTestReader(
        [URL],
        lambda _: None,
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
        **extra,
    )


def test_fallback_requests_json_and_uses_target_status(setup, monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=dict(status_code=200, headers={"Content-Type": "application/xml"}, body="<rss/>"),
        )

    instance = reader(monkeypatch, handler)
    result = instance.get(URL)
    assert result.ok and result.content == b"<rss/>"
    assert result.content_type == "application/xml"
    assert calls == [dict(zone="test-zone", url=URL, format="json")]
    assert instance.bright_requests == 1
    assert instance.bright_events[0]["direct_status"] == 403


@pytest.mark.parametrize(
    "response,expected",
    [
        (FetchResult(URL, 404, b"", None), False),
        (FetchResult(URL, 401, b"", None), False),
        (FetchResult(URL, 403, b"", None), True),
        (FetchResult(URL, 429, b"", None), True),
        (FetchResult(URL, 407, b"", None), True),
        (FetchResult(URL, 503, b"", None), True),
        (FetchResult(URL, 502, b"", None), True),
        (FetchResult(URL, 504, b"", None), True),
        (FetchResult(URL, 200, b"<title>Client Challenge</title>", None), True),
        (FetchResult(URL, 200, b'<script src="/_fs-ch-123/challenge.js"></script>', None), True),
        (FetchResult(URL, 200, b"<rss><channel/></rss>", None), False),
        (FetchResult(URL, 200, b"<html>cf-chl-platform</html>", None), True),
        (FetchResult(URL, None, b"", "Connection reset"), True),
        (FetchResult(URL, None, b"", "robots_disallowed: policy"), False),
        (FetchResult(URL, None, b"", "Endereço resolve para rede não pública"), False),
    ],
)
def test_when_to_use_paid_fallback(response, expected):
    assert should_fallback(response) == expected


def test_api_error_does_not_become_source_error_or_consume_repeated_requests(setup, monkeypatch):
    instance = reader(monkeypatch, lambda req: httpx.Response(401))
    first = instance.get(URL)
    instance.get(URL)
    assert instance.bright_requests == 1 and instance.bright_provider_error
    assert first.status is None
    assert classify(dict(bright_data_provider_error=True, outcome="blocked")) == "inconclusive"


def test_http_200_from_api_does_not_hide_target_block(setup, monkeypatch):
    instance = reader(
        monkeypatch,
        lambda req: httpx.Response(200, json=dict(status_code=403, headers={}, body="denied")),
    )
    assert instance.get(URL).status == 403


def test_proxy_auth_failure_is_provider_error(setup, monkeypatch):
    instance = reader(
        monkeypatch,
        lambda req: httpx.Response(200, json=dict(status_code=407, headers={}, body="")),
    )
    result = instance.get(URL)
    assert result.status is None
    assert instance.bright_provider_error
    assert "proxy HTTP 407" in result.error


def test_fallback_budget_stops_paid_calls(setup, monkeypatch):
    instance = reader(
        monkeypatch,
        lambda req: httpx.Response(200, json=dict(status_code=200, headers={}, body="<rss/>")),
        max_bright_requests=1,
    )
    assert instance.get(URL).ok
    assert instance.get(URL).status == 403
    assert instance.bright_requests == 1 and instance.bright_limit_reached


def test_provider_redirect_cannot_access_private_network(setup, monkeypatch):
    instance = reader(
        monkeypatch,
        lambda req: httpx.Response(
            200, json=dict(status_code=302, headers={"Location": "http://127.0.0.1/admin"}, body="")
        ),
    )
    response = instance.get(URL)
    assert not response.ok and instance.bright_requests == 1


def test_fallback_fetches_requested_url_without_robots(setup, monkeypatch):
    direct_calls, provider_calls = [], []

    def direct(self, url, **kwargs):
        direct_calls.append(url)
        return FetchResult(url, 403, b"", None, url)

    monkeypatch.setattr(TestReader, "_fetch", direct)

    def handler(request):
        provider_calls.append(json.loads(request.content)["url"])
        return httpx.Response(200, json=dict(status_code=200, headers={}, body="<rss/>"))

    instance = BrightTestReader(
        [URL],
        lambda _: None,
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    result = instance.get(URL)
    assert result.ok
    assert direct_calls == provider_calls == [URL]
    assert instance.bright_requests == 1


def test_structured_provider_response_has_size_limit(setup, monkeypatch):
    instance = reader(
        monkeypatch,
        lambda req: httpx.Response(200, json=dict(status_code=200, headers={}, body="x" * 100)),
        max_bytes=8,
    )
    assert not instance.get(URL).ok and instance.bright_limit_reached


def counting(monkeypatch, *statuses):
    """Acesso direto que devolve os status combinados, na ordem, e conta as tentativas."""
    asked = []

    def direct(self, url, **kwargs):
        asked.append(url)
        status = statuses[min(len(asked) - 1, len(statuses) - 1)]
        body = b"<rss/>" if status == 200 else b""
        return FetchResult(url, status, body, None, url)

    monkeypatch.setattr(TestReader, "_fetch", direct)
    monkeypatch.setattr(TestReader, "_allowed", lambda self, url: (True, ""))
    return asked


def paid(monkeypatch, handler, **extra):
    return BrightTestReader(
        [URL],
        lambda _: None,
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
        direct_pause=0,
        **extra,
    )


OK_PAID = lambda req: httpx.Response(  # noqa: E731
    200, json=dict(status_code=200, headers={"Content-Type": "application/xml"}, body="<rss/>")
)


def test_with_two_direct_attempts_the_provider_is_called_only_after_both_fail(setup, monkeypatch):
    asked = counting(monkeypatch, 403)
    instance = paid(monkeypatch, OK_PAID, direct_attempts=2)
    assert instance.get(URL).ok
    assert len(asked) == 2 and instance.bright_requests == 1


def test_a_second_direct_attempt_that_works_costs_nothing(setup, monkeypatch):
    asked = counting(monkeypatch, 503, 200)
    instance = paid(monkeypatch, OK_PAID, direct_attempts=2)
    assert instance.get(URL).ok
    assert len(asked) == 2 and instance.bright_requests == 0


def test_a_refusal_that_never_justifies_the_provider_is_not_retried(setup, monkeypatch):
    asked = counting(monkeypatch, 404)
    instance = paid(monkeypatch, OK_PAID, direct_attempts=2)
    assert instance.get(URL).status == 404
    assert len(asked) == 1 and instance.bright_requests == 0


def test_one_direct_attempt_is_still_the_default(setup, monkeypatch):
    asked = counting(monkeypatch, 403)
    instance = paid(monkeypatch, OK_PAID)
    assert instance.get(URL).ok
    assert len(asked) == 1 and instance.bright_requests == 1
