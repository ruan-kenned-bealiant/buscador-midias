"""Sessão pública, keep-alive, espera do servidor e ciclos sem chamadas ocultas."""

from datetime import UTC, datetime
from email.message import Message

import pytest

from buscador_midias.catalog.http_policy import retry_after, retryable
from buscador_midias.catalog.http_session import HttpSessions
from buscador_midias.catalog.reader import TestReader

ROOT = "https://news.example"


@pytest.fixture
def network(monkeypatch):
    sessions = HttpSessions()
    calls, connections, responses = [], [], {}
    monkeypatch.setattr("buscador_midias.catalog.reader.public_ip", lambda *args: "93.184.216.34")

    class Socket:
        def settimeout(self, seconds):
            pass

        def shutdown(self, how):
            pass

    class Response:
        will_close = False

        def __init__(self, status=200, body=b"<rss/>", **headers):
            self.status, self.body = status, body
            self.headers = Message()
            for key, value in headers.items():
                self.headers[key.replace("_", "-")] = value

        def getheader(self, name, default=None):
            return self.headers.get(name, default)

        def info(self):
            return self.headers

        def read1(self, size):
            body, self.body = self.body, b""
            return body

    class Connection:
        def __init__(self, host, ip, port, secure, timeout):
            self.host, self.sock, self.closed = host, Socket(), False
            connections.append(self)

        def request(self, method, path, headers):
            self.path = path
            calls.append((self.host, path, headers))

        def getresponse(self):
            data = responses.get(self.path, {})
            if isinstance(data, Exception):
                raise data
            return Response(**data)

        def close(self):
            self.closed = True

    monkeypatch.setattr("buscador_midias.catalog.reader._PinnedConnection", Connection)
    yield sessions, calls, connections, responses
    sessions.close()


def reader(sessions, root=ROOT):
    return TestReader([root], lambda _: None, sessions=sessions)


def test_connection_and_cookie_reused_only_for_same_site(network):
    sessions, calls, connections, responses = network
    responses["/feed"] = dict(Set_Cookie="public_session=abc; Secure; Path=/news")
    first = reader(sessions)
    assert first.get(ROOT + "/feed").ok
    second = reader(sessions)
    assert second.get(ROOT + "/news/story").ok
    assert second.connections_reused == 1 and second.cookies_sent == 1
    assert len(connections) == 1
    assert calls[-1][2]["Cookie"] == "public_session=abc"
    assert second.get(ROOT + "/elsewhere").ok
    assert "Cookie" not in calls[-1][2]
    # Mesmo domínio de destino, outra fonte: conexão e cookies isolados.
    other = TestReader(["https://another.example", ROOT], lambda _: None, sessions=sessions)
    assert other.get(ROOT + "/news/story").ok
    assert "Cookie" not in calls[-1][2] and len(connections) == 2
    insecure = reader(sessions, "http://news.example")
    assert insecure.get("http://news.example/news/story").ok
    assert "Cookie" not in calls[-1][2]


@pytest.mark.parametrize("two_hops", [False, True])
def test_redirect_cycles_stop_without_retrying_the_same_address(network, two_hops):
    sessions, calls, _, responses = network
    responses["/a"] = dict(status=301, Location=ROOT + ("/b" if two_hops else "/a"))
    responses["/b"] = dict(status=302, Location=ROOT + "/a")
    response = reader(sessions).get(ROOT + "/a")
    assert "redirect_cycle:" in response.error
    assert len(calls) == (2 if two_hops else 1)
    assert not retryable(response.status, response.error)


def test_retry_after_blocks_next_reader_without_new_request(network):
    sessions, calls, _, responses = network
    responses["/feed"] = dict(status=429, Retry_After="120")
    first = reader(sessions)
    response = first.get(ROOT + "/feed")
    assert response.retry_after_seconds == 120
    assert first.retry_after_host == "news.example"
    second = reader(sessions)
    assert not second.get(ROOT + "/another").ok
    assert second.cooldown_deferred and second.requests == 0
    assert len(calls) == 1


def test_dead_pooled_connection_does_not_trigger_hidden_http_retries(network):
    sessions, calls, connections, responses = network
    assert reader(sessions).get(ROOT + "/feed").ok
    responses["/second"] = OSError("Connection reset by peer")
    second = reader(sessions)
    assert not second.get(ROOT + "/second").ok
    assert second.requests == 1 and len(calls) == 2
    assert connections[0].closed
    responses["/second"] = {}
    assert reader(sessions).get(ROOT + "/second").ok
    assert len(connections) == 2


def test_expired_idle_connections_are_closed(network, monkeypatch):
    sessions, _, connections, _ = network
    clock = [100.0]
    monkeypatch.setattr("buscador_midias.catalog.http_session.time.monotonic", lambda: clock[0])
    assert reader(sessions).get(ROOT + "/feed").ok
    clock[0] += 31
    assert reader(sessions).get(ROOT + "/other").ok
    assert len(connections) == 2 and connections[0].closed


@pytest.mark.parametrize(
    "header,seconds",
    [
        ("120", 120),
        ("0", 0),
        ("Sun, 25 Oct 2026 12:02:00 GMT", 120),
        ("Sun, 25 Oct 2026 11:59:00 GMT", 0),
        ("-4", None),
        ("invalid", None),
        ("9999999999999999999999", None),
        (None, None),
    ],
)
def test_parse_retry_after(header, seconds):
    assert retry_after(header, datetime(2026, 10, 25, 12, tzinfo=UTC)) == seconds


@pytest.mark.parametrize("status,expected", [(403, False), (404, False), (429, True), (503, True)])
def test_http_error_classification(status, expected):
    assert retryable(status, None) is expected


def test_cookie_from_public_403_can_recover_on_next_bounded_attempt(network):
    from buscador_midias.ingestion.engine import Engine, RetryableError

    sessions, calls, _, responses = network
    responses["/feed"] = dict(status=403, Set_Cookie="public_session=abc; Secure; Path=/")
    first = reader(sessions)
    response = first.get(ROOT + "/feed")
    assert first.session_updated
    # A atualização de sessão permite nova tentativa; não é um 403 permanente.
    with pytest.raises(RetryableError) as caught:
        engine = object.__new__(Engine)
        engine.http_failure(first, response, "no XML")
    assert caught.value.retryable
    responses["/feed"] = {}
    second = reader(sessions)
    assert second.get(ROOT + "/feed").ok
    assert len(calls) == 2 and calls[-1][2]["Cookie"] == "public_session=abc"


def test_the_gap_between_two_accesses_to_a_host_can_be_widened(monkeypatch):
    """Notícias pedem mais respiro que feeds: o intervalo padrão é 300 ms e pode ser maior."""
    now = [100.0]
    slept: list[float] = []
    monkeypatch.setattr("buscador_midias.catalog.http_session.time.monotonic", lambda: now[0])
    monkeypatch.setattr("buscador_midias.catalog.http_session.time.sleep", slept.append)
    sessions = HttpSessions()

    sessions.wait("a.example", 60)
    sessions.wait("a.example", 60)
    sessions.wait("b.example", 60, gap=1.5)
    sessions.wait("b.example", 60, gap=1.5)

    assert slept == pytest.approx([0.3, 1.5])


def test_a_gap_larger_than_the_remaining_budget_is_refused_instead_of_slept():
    sessions = HttpSessions()
    sessions.wait("a.example", 60, gap=5)

    with pytest.raises(TimeoutError):
        sessions.wait("a.example", 1, gap=5)


def test_the_reader_carries_the_gap_it_was_given():
    assert TestReader([ROOT], lambda _: None).host_gap == 0.3
    assert TestReader([ROOT], lambda _: None, host_gap=1.5).host_gap == 1.5
