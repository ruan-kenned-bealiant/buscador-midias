"""Sonda de um link: o que o cadastro deveria fazer com ele, sem alterar nada."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.catalog.link_review import (
    apply_proposals,
    load_links,
    review_link,
    review_sites,
    review_with_retry,
    review_with_transports,
)
from buscador_midias.catalog.reader import TestLimitError
from buscador_midias.catalog.store import CatalogError, CatalogStore
from buscador_midias.net.fetcher import FetchResult

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


class Reader:
    """Leitor falso: devolve o resultado combinado e registra cada URL pedida."""

    def __init__(self, result: FetchResult | None = None) -> None:
        self.result = result
        self.asked: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.asked.append(url)
        return self.result or FetchResult(url, 200, b"", None)


def link(url="https://a.example/feed.xml", label="", **changes):
    base = dict(id=7, version=3, source_id=1, url=url, label=label, kind="feed", model="rss")
    base.update(changes)
    return base


def feed(*ages_in_days: int) -> bytes:
    items = "".join(
        f"<item><link>https://a.example/n{n}</link><pubDate>"
        f"{(NOW - timedelta(days=age)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for n, age in enumerate(ages_in_days)
    )
    return f"<rss><channel>{items}</channel></rss>".encode()


def ok(body: bytes) -> Reader:
    return Reader(FetchResult("https://a.example/feed.xml", 200, body, None))


def test_a_feed_with_news_this_week_is_kept():
    found = review_link(link(), ok(feed(2, 9)), NOW)
    assert found["proposal"] == "keep"
    assert found["age_days"] == 2
    assert found["basis"] == "publication"


def test_a_feed_stopped_for_two_months_is_inactivated():
    found = review_link(link(), ok(feed(60, 90)), NOW)
    assert found["proposal"] == "inactivate"
    assert found["age_days"] == 60


def test_a_feed_stopped_for_over_a_year_is_deleted():
    found = review_link(link(), ok(feed(400, 800)), NOW)
    assert found["proposal"] == "delete"


def test_the_report_carries_what_is_needed_to_apply_it_later():
    found = review_link(link(), ok(feed(2)), NOW)
    assert (found["link_id"], found["link_version"], found["url"]) == (
        7,
        3,
        "https://a.example/feed.xml",
    )
    assert found["entries"] == 1


def test_a_failed_access_is_never_a_reason_to_remove():
    reader = Reader(FetchResult("https://a.example/feed.xml", 403, b"", None))
    found = review_link(link(), reader, NOW)
    assert found["proposal"] == "review"
    assert "403" in found["reason"]


def test_a_network_failure_is_never_a_reason_to_remove():
    reader = Reader(FetchResult("https://a.example/feed.xml", None, b"", "timeout"))
    found = review_link(link(), reader, NOW)
    assert found["proposal"] == "review"
    assert "timeout" in found["reason"]


def test_a_page_that_is_not_xml_goes_to_review():
    found = review_link(link(), ok(b"<html><body>Bloqueado</body></html>"), NOW)
    assert found["proposal"] == "review"
    assert found["reason"]


def test_a_feed_without_any_date_goes_to_review():
    body = b"<rss><channel><item><link>https://a.example/n</link></item></channel></rss>"
    found = review_link(link(), ok(body), NOW)
    assert found["proposal"] == "review"
    assert "data" in found["reason"]


def test_a_useless_theme_is_removed_without_touching_the_network():
    reader = Reader()
    found = review_link(link("https://a.example/receitas/feed.xml"), reader, NOW)
    assert found["proposal"] == "remove_theme"
    assert found["theme"] == "cooking"
    assert reader.asked == []


def test_a_useless_theme_in_the_label_is_removed_too():
    reader = Reader()
    found = review_link(link(label="Horóscopo do dia"), reader, NOW)
    assert found["proposal"] == "remove_theme"
    assert reader.asked == []


def test_a_live_sports_link_goes_to_review_because_scandals_are_useful():
    found = review_link(link("https://a.example/esporte/feed.xml"), ok(feed(1)), NOW)
    assert found["proposal"] == "review"
    assert found["theme"] == "sports"


def test_a_dead_sports_link_is_still_inactivated_by_freshness():
    found = review_link(link("https://a.example/esporte/feed.xml"), ok(feed(60)), NOW)
    assert found["proposal"] == "inactivate"


def test_a_sitemap_index_is_judged_by_the_lastmod_of_its_children():
    body = (
        b"<sitemapindex><sitemap><loc>https://a.example/s1.xml</loc>"
        b"<lastmod>2024-01-01</lastmod></sitemap></sitemapindex>"
    )
    found = review_link(link("https://a.example/sitemap.xml", kind="sitemap"), ok(body), NOW)
    assert found["proposal"] == "delete"
    assert found["basis"] == "index_lastmod"
    assert found["exact"] is False


# --- Lote de sites: carga dos links, sondagem em paralelo e aplicação auditada --------------------


@pytest.fixture
def store(tmp_path):
    csv = tmp_path / "sites.csv"
    csv.write_text(
        "region,name,url,country,country_code,language,source_type\n"
        "América,Alfa,https://alfa.example,Brasil,BR,Português,portal\n"
        "América,Beta,https://beta.example,Brasil,BR,Português,portal\n"
    )
    snapshot = tmp_path / "retrato.json"
    snapshot.write_text(
        json.dumps(
            dict(
                sources=[
                    dict(key="alfa.example", name="Alfa"),
                    dict(key="beta.example", name="Beta"),
                ],
                mechanisms={},
            )
        )
    )
    catalog = CatalogStore(tmp_path / "catalogo.sqlite", snapshot, csv)
    catalog.initialize()
    return catalog


HOSTS = {1: "alfa.example", 2: "beta.example"}


def add_link(store, source_id, path, **changes):
    data = dict(url=f"https://{HOSTS[source_id]}/{path}", kind="feed", model="rss")
    data.update(changes)
    return store.save("endpoint", data, source_id=source_id)


class Fleet:
    """Um leitor falso por link: cada caminho tem a idade do último item combinada."""

    def __init__(self, ages: dict[str, int]) -> None:
        self.ages = ages
        self.asked: list[str] = []

    def __call__(self, link):
        fleet = self

        class One:
            def get(self, url):
                fleet.asked.append(url)
                path = url.rsplit("/", 1)[1]
                return FetchResult(url, 200, feed(fleet.ages[path]), None)

        return One()


def test_only_active_visible_links_of_the_chosen_sites_are_loaded(store):
    keep = add_link(store, 1, "vivo")
    dead = add_link(store, 1, "inativo", active=False)
    gone = add_link(store, 1, "excluido")
    store.delete("endpoint", gone["id"], gone["version"])
    other = add_link(store, 2, "outro-site")
    ids = {item["id"] for item in load_links(store, [1])}
    assert keep["id"] in ids
    assert not ids & {dead["id"], gone["id"], other["id"]}


def test_a_batch_reports_every_link_and_changes_nothing(store):
    a = add_link(store, 1, "novo")
    b = add_link(store, 1, "parado")
    c = add_link(store, 2, "morto")
    fleet = Fleet({"novo": 1, "parado": 90, "morto": 500})
    found = review_sites(store, [1, 2], NOW, fleet, per_host=2)
    assert {item["link_id"]: item["proposal"] for item in found} == {
        a["id"]: "keep",
        b["id"]: "inactivate",
        c["id"]: "delete",
    }
    versions = {a["id"]: a["version"], b["id"]: b["version"], c["id"]: c["version"]}
    for ident, version in versions.items():
        assert store.endpoint(ident)["version"] == version


def test_applying_inactivates_deletes_and_leaves_the_rest(store):
    keep = add_link(store, 1, "novo")
    stop = add_link(store, 1, "parado")
    dead = add_link(store, 1, "morto")
    theme = add_link(store, 1, "receitas")
    found = review_sites(store, [1], NOW, Fleet({"novo": 1, "parado": 90, "morto": 500}))
    done = apply_proposals(store, found)
    assert done["inactivate"] == 1 and done["delete"] == 1 and done["remove_theme"] == 1
    assert store.endpoint(keep["id"])["active"]
    assert not store.endpoint(stop["id"])["active"]
    with store.connection() as db:
        rows = {
            r["id"]: r["deleted_at"]
            for r in db.execute("SELECT id,deleted_at FROM links").fetchall()
        }
    assert rows[dead["id"]] and rows[theme["id"]]
    assert not rows[stop["id"]] and not rows[keep["id"]]


def test_a_link_edited_after_the_probe_is_skipped_not_overwritten(store):
    stop = add_link(store, 1, "parado")
    found = review_sites(store, [1], NOW, Fleet({"parado": 90}))
    store.save("endpoint", dict(version=stop["version"], notes="editado por pessoa"), stop["id"])
    done = apply_proposals(store, found)
    assert done["inactivate"] == 0
    assert done["conflict"] == 1
    assert store.endpoint(stop["id"])["active"]


def test_review_proposals_are_never_applied(store):
    add_link(store, 1, "sem-data")
    results = [
        dict(proposal="review", link_id=1, link_version=1, reason="acesso: HTTP 403"),
    ]
    done = apply_proposals(store, results)
    assert done["review"] == 1 and done["inactivate"] == done["delete"] == 0


def test_a_reader_that_runs_out_of_time_sends_the_link_to_review():
    class Slow:
        def get(self, url):
            raise TestLimitError("Tempo máximo da amostra esgotado.")

    found = review_link(link(), Slow(), NOW)
    assert found["proposal"] == "review"
    assert "Tempo" in found["reason"]


# --- Falha transitória: repetir com espera em vez de reprovar o link --------------------------


class Sequence:
    """Fábrica de leitores: cada tentativa recebe a próxima resposta combinada."""

    def __init__(self, *responses: FetchResult) -> None:
        self.responses = list(responses)
        self.made = 0

    def __call__(self, link):
        result = self.responses[min(self.made, len(self.responses) - 1)]
        self.made += 1
        return Reader(result)


def failure(status=None, error=None, retry_after=None):
    return FetchResult(
        "https://a.example/feed.xml", status, b"", error, retry_after_seconds=retry_after
    )


GOOD = FetchResult("https://a.example/feed.xml", 200, feed(2), None)


def run_retry(factory, attempts=3):
    waits: list[float] = []
    found = review_with_retry(link(), factory, NOW, attempts=attempts, sleep=waits.append)
    return found, waits


def test_a_transient_failure_is_retried_until_it_works():
    factory = Sequence(failure(503), failure(None, "timed out"), GOOD)
    found, waits = run_retry(factory)
    assert found["proposal"] == "keep"
    assert factory.made == 3 and len(waits) == 2


def test_a_failure_that_never_recovers_stays_in_review_after_all_attempts():
    factory = Sequence(failure(503))
    found, waits = run_retry(factory)
    assert found["proposal"] == "review" and "503" in found["reason"]
    assert factory.made == 3 and len(waits) == 2


@pytest.mark.parametrize("status", [400, 402, 403, 404, 406])
def test_a_refusal_is_not_retried(status):
    factory = Sequence(failure(status))
    found, waits = run_retry(factory)
    assert found["proposal"] == "review"
    assert factory.made == 1 and waits == []


def test_the_wait_asked_by_the_server_is_honoured_up_to_a_limit():
    factory = Sequence(failure(429, None, 7), failure(None, "host_cooldown: x", 900), GOOD)
    found, waits = run_retry(factory)
    assert found["proposal"] == "keep"
    assert waits == [7, 60]


def test_a_slow_dns_and_a_closed_connection_are_transient():
    for error in ("DNS excedeu o tempo de teste", "Remote end closed connection without response"):
        factory = Sequence(failure(None, error), GOOD)
        assert run_retry(factory)[0]["proposal"] == "keep"


def test_a_success_costs_a_single_attempt_and_no_wait():
    factory = Sequence(GOOD)
    found, waits = run_retry(factory)
    assert factory.made == 1 and waits == []


def test_a_useless_theme_never_builds_a_reader():
    factory = Sequence(GOOD)
    found = review_with_retry(
        link("https://a.example/receitas/feed.xml"), factory, NOW, sleep=lambda s: None
    )
    assert found["proposal"] == "remove_theme" and factory.made == 0


def test_loading_links_carries_the_access_transport_the_catalog_chose(store):
    plain = add_link(store, 1, "direto")
    browser = add_link(store, 1, "navegador", access_transport="curl_chrome_h2")
    by_id = {item["id"]: item for item in load_links(store, [1])}
    assert by_id[plain["id"]]["access_transport"] == "direct"
    assert by_id[browser["id"]]["access_transport"] == "curl_chrome_h2"


def test_a_closed_period_in_the_address_is_historical_and_never_read():
    reader = ok(feed(1))
    found = review_link(link(url="https://a.example/sitemap/2026/10/19_1.xml"), reader, NOW)
    assert found["proposal"] == "remove_historical"
    assert "2026-10-19" in found["reason"]
    assert reader.asked == []


def test_the_current_month_in_the_address_is_judged_by_its_content():
    found = review_link(link(url="https://a.example/sitemap-2026-10.xml"), ok(feed(1)), NOW)
    assert found["proposal"] == "keep"


def test_a_historical_link_is_never_even_given_a_reader():
    made = []

    def factory(item):
        made.append(item["url"])
        return ok(feed(1))

    found = review_with_retry(link(url="https://a.example/sitemap/2026/09/01.xml"), factory, NOW)
    assert found["proposal"] == "remove_historical" and made == []


def test_a_historical_link_is_soft_deleted_like_a_theme_link(store):
    old = add_link(store, 1, "sitemap/2026/09/01.xml")
    found = review_link(load_links(store, [1])[0], ok(feed(1)), NOW)
    done = apply_proposals(store, [found])
    assert done == {"remove_historical": 1}
    with pytest.raises(CatalogError, match="excluído"):
        store.endpoint(old["id"])


@pytest.mark.parametrize(
    "response, cause",
    [
        (FetchResult("u", 503, b"", None), "access"),
        (FetchResult("u", None, b"", "timed out"), "access"),
        (FetchResult("u", 200, b"<html>captcha</html>", None), "unreadable"),
        (FetchResult("u", 200, feed(), None), "undated"),
    ],
)
def test_every_review_outcome_carries_a_stable_cause(response, cause):
    found = review_link(link(), Reader(response), NOW)
    assert found["proposal"] == "review" and found["cause"] == cause


def test_the_sports_review_has_its_own_cause():
    found = review_link(link(url="https://a.example/esportes/feed.xml"), ok(feed(1)), NOW)
    assert found["proposal"] == "review" and found["cause"] == "sports"


class ByTransport:
    """Fábrica de leitores falsa: a resposta depende do transporte do link e fica registrada."""

    def __init__(self, answers, default=None):
        self.answers, self.default, self.asked = answers, default, []

    def __call__(self, item):
        factory = self
        transport = item.get("access_transport", "direct")

        class One:
            def get(self, url):
                factory.asked.append(transport)
                answer = factory.answers.get(transport, factory.default)
                return (
                    answer
                    if isinstance(answer, FetchResult)
                    else FetchResult(url, 200, answer, None)
                )

        return One()


CYCLE = FetchResult("u", None, b"", "redirect_cycle: ciclo de redirecionamento")


def test_a_refused_link_is_retried_with_other_transports_and_reports_the_one_that_worked():
    factory = ByTransport({"direct": CYCLE, "curl_chrome_h1": feed(1)}, default=CYCLE)
    found = review_with_transports(link(access_transport="direct"), factory, NOW)
    assert found["proposal"] == "keep"
    assert found["transport"] == "curl_chrome_h1" and found["transport_changed"] is True


@pytest.mark.parametrize("status", [401, 402, 403, 406])
def test_refusals_also_justify_trying_other_transports(status):
    refused = FetchResult("u", status, b"", None)
    factory = ByTransport({"curl_chrome_h2": feed(1)}, default=refused)
    found = review_with_transports(link(access_transport="curl_firefox_h2"), factory, NOW)
    assert found["proposal"] == "keep" and found["transport"] == "curl_chrome_h2"


@pytest.mark.parametrize(
    "response",
    [
        FetchResult("u", 404, b"", None),
        FetchResult("u", 410, b"", None),
        FetchResult("u", 503, b"", None),
        FetchResult("u", None, b"", "timed out"),
    ],
)
def test_gone_or_passing_failures_are_not_transport_problems(response):
    factory = ByTransport({}, default=response)
    found = review_with_transports(link(), factory, NOW, sleep=lambda s: None)
    assert found["proposal"] == "review" and not found.get("transport_changed")
    assert set(factory.asked) == {"direct"}


def test_when_no_transport_works_the_original_failure_is_reported():
    factory = ByTransport({}, default=CYCLE)
    found = review_with_transports(link(access_transport="direct"), factory, NOW)
    assert found["proposal"] == "review" and found["cause"] == "access"
    assert found["transport"] == "direct" and not found.get("transport_changed")
    assert set(factory.asked) == {"direct", "curl_chrome_h1", "curl_chrome_h2", "curl_firefox_h2"}


def test_a_working_link_never_tries_other_transports():
    factory = ByTransport({"direct": feed(1)})
    found = review_with_transports(link(), factory, NOW)
    assert found["proposal"] == "keep" and not found.get("transport_changed")
    assert factory.asked == ["direct"]


def test_a_transport_that_returns_a_captcha_page_is_not_a_fix():
    factory = ByTransport({"curl_chrome_h1": b"<html>captcha</html>"}, default=CYCLE)
    found = review_with_transports(link(), factory, NOW)
    assert found["cause"] == "access" and not found.get("transport_changed")


def test_the_current_transport_is_not_tried_twice_and_bot_profiles_are_never_used():
    factory = ByTransport({}, default=CYCLE)
    review_with_transports(link(access_transport="curl_chrome_h1"), factory, NOW)
    assert factory.asked.count("curl_chrome_h1") == 1
    assert not any("bot" in t for t in factory.asked)
