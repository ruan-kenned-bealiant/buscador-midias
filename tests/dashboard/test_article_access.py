"""Download da notícia: um 403 no transporte do link tenta os outros e lembra o que funcionou."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from buscador_midias.ingestion.article_access import ArticleAccess
from buscador_midias.net.fetcher import FetchResult

URL = "https://news.example/a"
OK = FetchResult(URL, 200, b"<html>texto</html>", None, URL, "text/html")


def refused(status: int = 403) -> FetchResult:
    return FetchResult(URL, status, b"", None, URL, "text/html")


class Reader:
    def __init__(self, transport: str, answer: FetchResult, log: list[str]) -> None:
        self.transport, self.answer, self.log = transport, answer, log
        self.cooldown_deferred = False

    def get(self, url: str) -> FetchResult:
        self.log.append(f"get:{self.transport}")
        return self.answer


class World:
    """Abre leitores por transporte, responde como combinado e registra abre/fecha/lê."""

    def __init__(self, answers: dict[str, FetchResult], default: FetchResult | None = None) -> None:
        self.answers, self.default = answers, default or refused()
        self.log: list[str] = []
        self.opened: list[str] = []
        self.deferred = False

    @contextmanager
    def open(self, transport: str):
        self.opened.append(transport)
        self.log.append(f"open:{transport}")
        reader = Reader(transport, self.answers.get(transport, self.default), self.log)
        reader.cooldown_deferred = self.deferred
        try:
            yield reader
        finally:
            self.log.append(f"close:{transport}")


def fetch(access: ArticleAccess, world: World, url: str = URL, base: str = "direct") -> Any:
    return access.get(url, base, world.open)


def test_a_story_that_opens_is_read_once_with_the_link_transport():
    world = World({"direct": OK})

    got = fetch(ArticleAccess(), world)

    assert got.response.ok and got.attempts == ("direct",)
    assert world.opened == ["direct"]


def test_a_403_is_retried_with_the_next_transport_until_one_opens():
    world = World({"direct": refused(), "curl_chrome_h2": refused(), "curl_chrome_h1": OK})

    got = fetch(ArticleAccess(), world)

    assert got.response.ok and got.transport == "curl_chrome_h1"
    assert got.attempts == ("direct", "curl_chrome_h2", "curl_chrome_h1")
    assert got.fallback is True


def test_each_reader_is_closed_before_the_next_opens_so_the_host_slot_is_free():
    world = World({"curl_chrome_h2": OK})

    fetch(ArticleAccess(), world)

    assert world.log == [
        "open:direct", "get:direct", "close:direct",
        "open:curl_chrome_h2", "get:curl_chrome_h2", "close:curl_chrome_h2",
    ]  # fmt: skip


def test_when_nothing_opens_the_original_refusal_is_what_comes_back():
    world = World({})

    got = fetch(ArticleAccess(), world)

    assert not got.response.ok and got.response.status == 403
    assert got.transport == "direct" and got.fallback is False
    assert got.attempts == ("direct", "curl_chrome_h2", "curl_chrome_h1", "curl_firefox_h2")


def test_only_a_403_triggers_the_search_for_another_transport():
    for status in (401, 404, 410, 429, 500, 503):
        world = World({"direct": refused(status)})

        got = fetch(ArticleAccess(), world)

        assert got.response.status == status and world.opened == ["direct"], status


def test_a_different_error_from_an_alternative_stops_the_search():
    world = World({"direct": refused(), "curl_chrome_h2": refused(404)})

    got = fetch(ArticleAccess(), world)

    assert world.opened == ["direct", "curl_chrome_h2"]
    assert got.response.status == 403  # a recusa original, não o 404 da tentativa


def test_a_link_already_on_a_curl_transport_skips_that_one():
    world = World({"curl_firefox_h2": OK})

    got = fetch(ArticleAccess(), world, base="curl_chrome_h1")

    assert world.opened == ["curl_chrome_h1", "curl_chrome_h2", "curl_firefox_h2"]
    assert got.transport == "curl_firefox_h2"


def test_the_transport_that_worked_is_tried_first_for_the_next_story_of_that_site():
    access = ArticleAccess()
    first = World({"curl_chrome_h2": OK})
    fetch(access, first)

    second = World({"curl_chrome_h2": OK})
    got = fetch(access, second, url="https://news.example/outra")

    assert second.opened == ["curl_chrome_h2"] and got.attempts == ("curl_chrome_h2",)
    assert got.fallback is False


def test_another_site_still_starts_with_its_own_link_transport():
    access = ArticleAccess()
    fetch(access, World({"curl_chrome_h2": OK}))

    other = World({"direct": OK})
    fetch(access, other, url="https://outro.example/x")

    assert other.opened == ["direct"]


def test_a_remembered_transport_that_stops_working_is_forgotten():
    access = ArticleAccess()
    fetch(access, World({"curl_chrome_h2": OK}))

    fetch(access, World({}), url="https://news.example/b")  # agora nada abre
    again = World({"direct": OK})
    fetch(access, again, url="https://news.example/c")

    assert again.opened == ["direct"]


def test_a_host_in_cooldown_is_not_hammered_with_more_attempts():
    world = World({})
    world.deferred = True

    got = fetch(ArticleAccess(), world)

    assert world.opened == ["direct"] and got.attempts == ("direct",)


def test_the_memory_of_working_transports_is_bounded():
    access = ArticleAccess(remember=2)
    for n in range(3):
        fetch(access, World({"curl_chrome_h2": OK}), url=f"https://s{n}.example/x")

    forgotten = World({"direct": OK})
    fetch(access, forgotten, url="https://s0.example/y")

    assert forgotten.opened == ["direct"]  # o mais antigo saiu da memória


def test_the_metrics_count_the_extra_requests_and_the_fallbacks_as_numbers_the_cost_keeps():
    """O custo da etapa só guarda números; os nomes dos transportes vão junto para o diagnóstico."""
    plain = fetch(ArticleAccess(), World({"direct": OK}))
    fallback = fetch(ArticleAccess(), World({"curl_chrome_h1": OK}))
    nothing = fetch(ArticleAccess(), World({}))

    assert plain.metrics() == {}
    assert fallback.metrics() == dict(
        transport_attempts=["direct", "curl_chrome_h2", "curl_chrome_h1"],
        transport_extra_requests=2,
        transport_fallback="curl_chrome_h1",
        transport_fallbacks=1,
    )
    assert nothing.metrics()["transport_extra_requests"] == 3
    assert "transport_fallbacks" not in nothing.metrics()


# --- disjuntor por site ------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def blocked_site(access: ArticleAccess, times: int, url: str = URL) -> list[World]:
    """Faz ``times`` notícias do site serem recusadas por todos os transportes."""
    worlds = [World({}) for _ in range(times)]
    for world in worlds:
        fetch(access, world, url=url)
    return worlds


def test_after_three_refusals_in_a_row_the_site_rests_and_no_request_is_made():
    access = ArticleAccess(clock=Clock())
    blocked_site(access, 3)

    resting = World({"direct": OK})
    got = fetch(access, resting)

    assert resting.opened == [] and got.attempts == ()
    assert got.response.status == 403 and not got.response.ok
    assert got.metrics() == dict(transport_skipped=1)


def test_the_rest_does_not_touch_other_sites():
    access = ArticleAccess(clock=Clock())
    blocked_site(access, 3)

    other = World({"direct": OK})
    got = fetch(access, other, url="https://outro.example/x")

    assert got.response.ok and other.opened == ["direct"]


def test_the_site_is_tried_again_when_the_rest_is_over():
    clock = Clock()
    access = ArticleAccess(clock=clock, pause=1200)
    blocked_site(access, 3)

    clock.now += 1199
    assert fetch(access, World({"direct": OK})).attempts == ()
    clock.now += 2
    again = World({"direct": OK})

    assert fetch(access, again).response.ok and again.opened == ["direct"]


def test_a_success_in_between_clears_the_count_of_refusals():
    access = ArticleAccess(clock=Clock())
    blocked_site(access, 2)
    fetch(access, World({"direct": OK}))  # abriu: zera a contagem
    blocked_site(access, 2)

    still_open = World({"direct": OK})

    assert fetch(access, still_open).response.ok  # só 2 seguidas: o site não descansa


def test_only_a_403_refused_by_every_transport_counts_toward_the_rest():
    access = ArticleAccess(clock=Clock())
    for status in (401, 404, 429, 500):
        for _ in range(3):
            fetch(access, World({"direct": refused(status)}))

    world = World({"direct": OK})

    assert fetch(access, world).response.ok  # nenhum desses contou


def test_a_host_in_cooldown_does_not_count_either():
    access = ArticleAccess(clock=Clock())
    for _ in range(3):
        deferred = World({})
        deferred.deferred = True
        fetch(access, deferred)

    assert fetch(access, World({"direct": OK})).response.ok


def test_a_story_opened_by_a_fallback_also_clears_the_count():
    access = ArticleAccess(clock=Clock())
    blocked_site(access, 2)
    fetch(access, World({"curl_chrome_h2": OK}))
    blocked_site(access, 2)

    assert fetch(access, World({"curl_chrome_h2": OK, "direct": OK})).response.ok
