"""Testes reais dos modelos com rede simulada, persistência e limites explícitos."""

import gzip
import json
import socket
import threading
import time

import pytest

from buscador_midias.catalog.models import get_model, list_models
from buscador_midias.catalog.reader import TestLimitError, TestReader, public_ip
from buscador_midias.catalog.store import CatalogError, CatalogStore
from buscador_midias.catalog.testing import TestJobs, run_test
from buscador_midias.net.fetcher import FetchResult

ROOT = "https://news.example/"
ARTICLE = ROOT + "noticia/acontecimentos-na-cidade"
SOURCE = dict(id=1, name="Notícias", url=ROOT)


def article(schema="NewsArticle"):
    data = dict(
        **{"@type": schema},
        headline="Reportagem sobre os acontecimentos na cidade",
        datePublished="2001-01-01T12:00:00Z",
    )
    text = (
        "A reportagem ouviu moradores e autoridades sobre os acontecimentos na cidade. "
        "Os documentos apresentados detalham os impactos das medidas anunciadas. "
    )
    return (
        '<html><head><meta charset="utf-8"><title>Reportagem sobre os acontecimentos na cidade'
        '</title><script type="application/ld+json">' + json.dumps(data) + "</script></head><body>"
        "<article><h1>Reportagem sobre os acontecimentos na cidade</h1>"
        + "".join("<p>" + text + "</p>" for _ in range(15))
        + "</article></body></html>"
    ).encode()


def feed():
    return (
        f"<rss><channel><item><link>{ARTICLE}</link>"
        "<pubDate>2001-01-01</pubDate></item></channel></rss>"
    ).encode()


def sitemap(urls=None):
    return (
        "<urlset>"
        + "".join("<url><loc>" + u + "</loc></url>" for u in (urls or [ARTICLE]))
        + "</urlset>"
    ).encode()


def index(urls):
    return (
        "<sitemapindex>"
        + "".join("<sitemap><loc>" + u + "</loc></sitemap>" for u in urls)
        + "</sitemapindex>"
    ).encode()


class FakeReader:
    def __init__(self, data, progress):
        self.data, self.progress, self.requests = data, progress, 0

    def get(self, url):
        self.requests += 1
        self.progress(dict(stage="http", message="HTTP simulado", url=url))
        response = self.data.get(url)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, FetchResult):
            return response
        return FetchResult(
            url,
            200 if response is not None else 404,
            response or b"",
            None,
            final_url=url,
            content_type="text/html" if url == ARTICLE else "text/xml",
        )


def run(data, model="auto", kind="sitemap", url=ROOT + "map.xml", parameters=None):
    return run_test(
        dict(test_id="test", url=url, model=model, kind=kind, parameters=parameters or {}),
        SOURCE,
        lambda s: None,
        lambda roots, progress: FakeReader(data, progress),
    )


@pytest.mark.parametrize("model", ["auto", "rss"])
def test_old_news_from_rss_is_text_not_error(model):
    result = run({ROOT + "map.xml": feed(), ARTICLE: article()}, model, "feed")
    assert result["outcome"] == "text_confirmed"
    assert result["news_urls"] == result["text_samples"] == 1
    assert result["root_entries"] == 1 and result["compatible"]


@pytest.mark.parametrize("body", [sitemap(), gzip.compress(sitemap())])
def test_sitemap_plain_or_gzip(body):
    result = run({ROOT + "map.xml": body, ARTICLE: article()}, "sitemap")
    assert result["outcome"] == "text_confirmed"


def test_index_root_counts_children_not_articles_and_deduplicates_cycles():
    root = ROOT + "map.xml"
    child = ROOT + "child.xml"
    result = run(
        {root: index([root, child, child]), child: sitemap(), ARTICLE: article()}, "sitemap_tree"
    )
    assert result["outcome"] == "text_confirmed"
    assert result["root_entries"] == 0 and result["root_children"] == 2
    assert result["xml_documents"] == 2 and result["requests"] == 3


def test_depth_ten_is_read_but_eleven_is_not():
    data = {ROOT + "map.xml": index([ROOT + "1.xml"])}
    for depth in range(1, 11):
        data[ROOT + str(depth) + ".xml"] = index([ROOT + str(depth + 1) + ".xml"])
    data[ROOT + "11.xml"] = sitemap()
    result = run(data, "sitemap_tree")
    assert result["xml_valid"] and result["news_urls"] == 0 and not result["complete"]
    assert result["xml_documents"] == 11
    assert any("máxima 10" in reason for reason in result["warnings"])


def test_index_document_limit_is_partial_not_infinite():
    root = ROOT + "map.xml"
    children = [ROOT + str(i) + ".xml" for i in range(20)]
    data = {root: index(children), **{u: b"<urlset/>" for u in children}}
    result = run(data, "sitemap_tree")
    assert result["xml_documents"] == 12 and not result["complete"]
    assert result["outcome"] == "xml_only"


@pytest.mark.parametrize(
    "model,kind,body",
    [
        ("rss", "feed", sitemap()),
        ("sitemap", "sitemap", index([ROOT + "child.xml"])),
        ("news_sitemap", "sitemap", sitemap()),
    ],
)
def test_wrong_model_never_confirms_functional_proof(model, kind, body):
    result = run({ROOT + "map.xml": body}, model, kind)
    assert result["xml_valid"] and not result["compatible"]
    assert result["outcome"] == "incompatible" and not result["samples"]


def test_news_sitemap_requires_news_metadata_not_recent_date():
    body = (
        '<urlset xmlns:n="http://www.google.com/schemas/sitemap-news/0.9">'
        f"<url><loc>{ARTICLE}</loc><n:news><n:title>Notícia antiga</n:title>"
        "<n:publication_date>2001-01-01</n:publication_date></n:news></url></urlset>"
    ).encode()
    assert (
        run({ROOT + "map.xml": body, ARTICLE: article()}, "news_sitemap")["outcome"]
        == "text_confirmed"
    )


def test_empty_valid_xml_is_successful_reading():
    result = run({ROOT + "map.xml": b"<urlset/>"}, "sitemap")
    assert result["outcome"] == "empty" and result["xml_valid"] and result["compatible"]


def test_html_200_is_not_functional_xml():
    result = run({ROOT + "map.xml": b"<html><body>Access denied</body></html>"})
    assert result["outcome"] == "invalid" and not result["xml_valid"]


def test_xml_with_blocked_articles_still_has_xml_evidence():
    result = run({ROOT + "map.xml": sitemap(), ARTICLE: FetchResult(ARTICLE, 403, b"", None)})
    assert result["xml_valid"] and result["outcome"] == "xml_only"
    assert result["samples"][0]["status"] == 403 and result["text_samples"] == 0


def test_audio_only_is_not_textual_news():
    result = run({ROOT + "map.xml": feed(), ARTICLE: article("PodcastEpisode")}, "rss", "feed")
    assert result["outcome"] == "audio_only" and result["text_samples"] == 0


def test_article_outside_source_never_qualifies():
    result = run({ROOT + "map.xml": sitemap(["https://other.example/article"])})
    assert result["outcome"] == "xml_only" and result["news_urls"] == 0 and not result["samples"]


def test_auto_detects_type_without_requiring_second_test():
    result = run({ROOT + "map.xml": sitemap(), ARTICLE: article()}, "auto", "feed")
    assert result["compatible"] and result["requested_kind"] == "sitemap"


def test_budget_after_valid_xml_keeps_partial_result():
    result = run({ROOT + "map.xml": sitemap(), ARTICLE: TestLimitError("Tempo esgotado")})
    assert result["outcome"] == "xml_only" and not result["complete"]


def test_model_validation():
    assert len(list_models()) == 5
    for value in [None, [], "unknown"]:
        with pytest.raises(CatalogError):
            get_model(value)
    with pytest.raises(CatalogError):
        get_model("rss", "sitemap")


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::1"]])
def test_dns_private_or_mixed_never_connects(monkeypatch, addresses):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args: [(0, 0, 0, "", (ip, 0)) for ip in addresses]
    )
    with pytest.raises(OSError):
        public_ip("news.example", 1)


def test_no_dns_rebinding_second_lookup(monkeypatch):
    calls = []

    def resolve(*args):
        calls.append(1)
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert public_ip("news.example", 1) == "93.184.216.34"
    assert len(calls) == 1


def test_robots_disallow_stops_before_xml(monkeypatch):
    reader = TestReader([ROOT], lambda step: None)
    # Obter a política pode ser simulado isoladamente; a URL do XML não é requisitada.
    from buscador_midias.discovery.robots import RobotsPolicy

    reader.policies["https://news.example"] = RobotsPolicy.parse("User-agent: *\nDisallow: /")
    monkeypatch.setattr(
        "buscador_midias.catalog.reader.public_ip", lambda *args: pytest.fail("Não deve conectar")
    )
    assert "robots_disallowed" in reader.get(ROOT + "map.xml").error
    assert reader.requests == 0


def test_redirect_to_private_or_unrelated_host_rejected_before_connect(monkeypatch):
    reader = TestReader([ROOT], lambda step: None)
    from buscador_midias.discovery.robots import RobotsPolicy

    reader.policies["https://news.example"] = RobotsPolicy.allow_all()
    monkeypatch.setattr("buscador_midias.catalog.reader.public_ip", lambda *args: "93.184.216.34")

    class Response:
        status = 302

        def getheader(self, name, default=None):
            return "http://127.0.0.1/secret" if name == "Location" else default

    class Connection:
        def __init__(self, *args):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("buscador_midias.catalog.reader._PinnedConnection", Connection)
    result = reader.get(ROOT + "map.xml")
    assert result.error and reader.requests == 1


@pytest.fixture
def catalog(tmp_path):
    sites = tmp_path / "sites.csv"
    sites.write_text(
        "region,name,url,country,country_code,language,source_type\nX,Notícias,https://news.example,Brasil,BR,pt,portal\n"
    )
    snapshot = tmp_path / "retrato.json"
    snapshot.write_text(
        json.dumps(dict(sources=[dict(key="news.example", name="Notícias")], mechanisms={}))
    )
    return CatalogStore(tmp_path / "catalogo.sqlite", snapshot, sites)


def await_result(jobs, ident):
    for _ in range(200):
        value = jobs.get(ident)
        if value["state"] != "running":
            assert value["state"] == "completed", value
            return value["result"]
        time.sleep(0.01)
    pytest.fail("Teste simulado não terminou")


def test_job_persisted_on_same_revision_and_draft_on_save(catalog):
    source = catalog.sources()["items"][0]
    jobs = TestJobs(
        catalog, lambda roots, p: FakeReader({ROOT + "map.xml": sitemap(), ARTICLE: article()}, p)
    )
    data = dict(source_id=source["id"], url=ROOT + "map.xml", kind="sitemap", model="sitemap")
    try:
        job = jobs.start(data)
        result = await_result(jobs, job["id"])
        assert catalog.endpoints(source["id"])["total"] == 0  # Rascunho não é cadastro.
        row = catalog.save(
            "endpoint",
            dict(url=data["url"], kind="sitemap", model="sitemap"),
            source_id=source["id"],
            test_result=jobs.result(job["id"]),
        )
        endpoint = catalog.endpoint(row["id"])
        assert endpoint["last_test"]["outcome"] == "text_confirmed"
        assert endpoint["last_test"]["usable_for_news"]
        assert len(endpoint["last_test"]["checks"]) == 3
        assert endpoint["evidence"]["texts"] == 1
        # Teste de um cadastro salva o resultado automaticamente e incrementa revisão.
        job = jobs.start(dict(data, endpoint_id=row["id"], version=row["version"]))
        result = await_result(jobs, job["id"])
        assert result["applied_version"] == 2
        assert catalog.endpoint(row["id"])["last_test"]["outcome"] == "text_confirmed"
        # Modelo alterado invalida a evidência sem destruir o evento anterior.
        catalog.save("endpoint", dict(model="auto", version=2), row["id"])
        assert catalog.endpoint(row["id"])["evidence"] == {}
        assert catalog.endpoint(row["id"])["last_test"] == {}
    finally:
        jobs.close()


def test_test_cannot_overwrite_edit_or_deleted_source(catalog):
    gate = threading.Event()
    started = threading.Event()
    source = catalog.sources()["items"][0]
    row = catalog.save(
        "endpoint", dict(url=ROOT + "map.xml", kind="sitemap"), source_id=source["id"]
    )

    class WaitingReader(FakeReader):
        def get(self, url):
            started.set()
            gate.wait(3)
            return super().get(url)

    jobs = TestJobs(
        catalog,
        lambda roots, p: WaitingReader({ROOT + "map.xml": sitemap(), ARTICLE: article()}, p),
    )
    try:
        job = jobs.start(
            dict(
                source_id=source["id"],
                url=row["url"],
                kind=row["kind"],
                model=row["model"],
                endpoint_id=row["id"],
                version=1,
            )
        )
        assert started.wait(1)
        catalog.save("endpoint", dict(version=1, notes="Não sobrescrever"), row["id"])
        gate.set()
        result = await_result(jobs, job["id"])
        assert result["applied_version"] is None and result["save_warning"]
        assert catalog.endpoint(row["id"])["last_test"] == {}
        assert catalog.endpoint(row["id"])["notes"] == "Não sobrescrever"
    finally:
        gate.set()
        jobs.close()


def test_maximum_two_running_jobs(catalog):
    source = catalog.sources()["items"][0]
    gate = threading.Event()

    class WaitingReader(FakeReader):
        def get(self, url):
            gate.wait(3)
            return super().get(url)

    jobs = TestJobs(catalog, lambda roots, p: WaitingReader({ROOT + "map.xml": b"<urlset/>"}, p))
    data = dict(source_id=source["id"], url=ROOT + "map.xml", kind="sitemap", model="auto")
    try:
        a, b = jobs.start(data), jobs.start(data)
        with pytest.raises(CatalogError) as error:
            jobs.start(data)
        assert error.value.status == 429
        gate.set()
        await_result(jobs, a["id"])
        await_result(jobs, b["id"])
    finally:
        gate.set()
        jobs.close()


def test_connection_uses_pinned_ip_but_tls_original_name(monkeypatch):
    from buscador_midias.catalog.reader import _PinnedConnection

    calls = []

    class Socket:
        def close(self):
            pass

    sock = Socket()

    def connect(address, timeout):
        calls.append((address, timeout))
        return sock

    class Context:
        def wrap_socket(self, raw, server_hostname):
            assert raw is sock and server_hostname == "news.example"
            return sock

    monkeypatch.setattr("socket.create_connection", connect)
    monkeypatch.setattr("ssl.create_default_context", lambda: Context())
    connection = _PinnedConnection("news.example", "93.184.216.34", 443, True, 3)
    connection.connect()
    assert calls == [(("93.184.216.34", 443), 3)]


def test_size_limit_is_inconclusive_not_site_failure():
    body = gzip.compress(b" " * (9 * 1024 * 1024))
    result = run({ROOT + "map.xml": body})
    assert result["outcome"] == "inconclusive" and not result["complete"]


def test_watchdog_interrupts_slow_response_headers(monkeypatch):
    from buscador_midias.discovery.robots import RobotsPolicy

    aborted = threading.Event()

    class SlowSocket:
        def shutdown(self, how):
            aborted.set()

    class SlowConnection:
        def __init__(self, *args):
            self.sock = SlowSocket()

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            aborted.wait(1)
            raise OSError("Leitura interrompida")

        def close(self):
            pass

    monkeypatch.setattr("buscador_midias.catalog.reader.public_ip", lambda *args: "93.184.216.34")
    monkeypatch.setattr("buscador_midias.catalog.reader._PinnedConnection", SlowConnection)
    reader = TestReader([ROOT], lambda s: None, seconds=0.05)
    reader.policies["https://news.example"] = RobotsPolicy.allow_all()
    start = time.monotonic()
    with pytest.raises(TestLimitError):
        reader.get(ROOT + "map.xml")
    assert aborted.is_set() and time.monotonic() - start < 0.5


def test_mechanism_settings_persist_and_do_not_change_other_models(catalog):
    original = catalog.model_settings("rss")
    assert original["parameters"]["article_samples"] == 3
    result = catalog.save_model(
        "rss", dict(version=1, parameters=dict(article_samples=1, timeout_seconds=20))
    )
    assert result["version"] == 2 and result["parameters"]["article_samples"] == 1
    assert result["parameters"]["max_requests"] == 24
    reopened = CatalogStore(catalog.path, catalog.snapshot, catalog.sites)
    assert reopened.model_settings("rss") == result
    assert reopened.model_settings("auto")["parameters"]["article_samples"] == 3
    again = catalog.save_model("rss", dict(version=2, parameters=dict(article_samples=1)))
    assert again["version"] == 2  # Salvar o mesmo valor não cria uma nova revisão.
    with catalog.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE entity='mechanism'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "model,parameters",
    [
        ("rss", {"article_samples": True}),
        ("rss", {"article_samples": 4}),
        ("rss", {"article_samples": -1}),
        ("rss", {"timeout_seconds": 0}),
        ("rss", {"timeout_seconds": 61}),
        ("rss", {"max_response_mib": 17}),
        ("rss", {"max_requests": 1000}),
        ("rss", {"sample_order": "random-code"}),
        ("rss", {"max_depth": 2}),
        ("sitemap_tree", {"max_depth": 11}),
        ("sitemap_tree", {"max_depth": -1}),
        ("auto", {"max_documents": 25}),
        ("auto", {"ignore_robots": True}),
    ],
)
def test_settings_reject_invalid_values_without_partial_update(catalog, model, parameters):
    before = catalog.model_settings(model)
    with pytest.raises(CatalogError):
        catalog.save_model(model, dict(version=before["version"], parameters=parameters))
    assert catalog.model_settings(model) == before


def test_model_settings_optimistic_revision(catalog):
    catalog.save_model("auto", dict(version=1, parameters=dict(max_depth=2)))
    with pytest.raises(CatalogError) as stale:
        catalog.save_model("auto", dict(version=1, parameters=dict(max_depth=3)))
    assert stale.value.status == 409
    assert catalog.model_settings("auto")["parameters"]["max_depth"] == 2


def test_configured_depth_and_document_limits_are_used():
    root = ROOT + "map.xml"
    child = ROOT + "child.xml"
    data = {root: index([child]), child: sitemap(), ARTICLE: article()}
    shallow = run(data, model="sitemap_tree", parameters=dict(max_depth=0))
    assert shallow["xml_documents"] == 1 and not shallow["samples"]
    limited = run(data, model="sitemap_tree", parameters=dict(max_documents=1))
    assert limited["xml_documents"] == 1 and not limited["complete"]
    full = run(data, model="sitemap_tree", parameters=dict(max_documents=2, max_depth=1))
    assert full["text_samples"] == 1 and full["xml_documents"] == 2


@pytest.mark.parametrize("order,expected", [("first", "0"), ("last", "3"), ("distributed", "0")])
def test_article_sampling_order_is_configurable(order, expected):
    urls = [ROOT + "noticia/" + str(i) for i in range(4)]
    result = run(
        {ROOT + "map.xml": sitemap(urls), **{u: article() for u in urls}},
        parameters=dict(article_samples=1, sample_order=order),
    )
    assert (
        len(result["samples"]) == 1 and result["samples"][0]["url"] == ROOT + "noticia/" + expected
    )


def test_zero_samples_only_checks_xml():
    result = run(
        {ROOT + "map.xml": sitemap(), ARTICLE: article()}, parameters=dict(article_samples=0)
    )
    assert result["xml_valid"] and result["news_urls"] == 1 and result["outcome"] == "xml_only"
    assert result["requests"] == 1 and not result["samples"]


def test_configured_size_is_enforced_on_compressed_xml():
    body = gzip.compress(b" " * (2 * 1024 * 1024))
    result = run({ROOT + "map.xml": body}, parameters=dict(max_response_mib=1))
    assert result["outcome"] == "inconclusive"
    assert any("1 MiB" in message for message in result["warnings"])


def test_reader_receives_configured_network_limits(monkeypatch):
    captured = {}

    class ConfiguredReader(FakeReader):
        def __init__(self, roots, progress, **limits):
            captured.update(limits)
            super().__init__({ROOT + "map.xml": b"<urlset/>"}, progress)

    monkeypatch.setattr("buscador_midias.catalog.testing.TestReader", ConfiguredReader)
    result = run_test(
        dict(
            test_id="wire",
            url=ROOT + "map.xml",
            model="sitemap",
            kind="sitemap",
            parameters=dict(timeout_seconds=9, max_requests=7, max_response_mib=2),
        ),
        SOURCE,
        lambda s: None,
    )
    assert captured == dict(seconds=9, requests=7, max_bytes=2 * 1024 * 1024)
    assert result["parameters"]["timeout_seconds"] == 9


def test_changed_model_revision_does_not_receive_running_test(catalog):
    gate = threading.Event()
    started = threading.Event()
    source = catalog.sources()["items"][0]
    row = catalog.save(
        "endpoint", dict(url=ROOT + "map.xml", kind="sitemap"), source_id=source["id"]
    )

    class WaitingReader(FakeReader):
        def get(self, url):
            started.set()
            gate.wait(3)
            return super().get(url)

    jobs = TestJobs(
        catalog,
        lambda roots, p: WaitingReader({ROOT + "map.xml": sitemap(), ARTICLE: article()}, p),
    )
    try:
        job = jobs.start(
            dict(
                source_id=source["id"],
                url=row["url"],
                kind="sitemap",
                model="auto",
                endpoint_id=row["id"],
                version=1,
            )
        )
        assert started.wait(1)
        catalog.save_model("auto", dict(version=1, parameters=dict(article_samples=0)))
        gate.set()
        result = await_result(jobs, job["id"])
        assert result["model_version"] == 1 and result["parameters"]["article_samples"] == 3
        assert result["outcome"] == "text_confirmed" and result["save_warning"]
        assert catalog.endpoint(row["id"])["last_test"] == {}
        with pytest.raises(CatalogError) as stale:
            catalog.save(
                "endpoint",
                dict(url=ROOT + "another.xml", kind="sitemap", model="auto"),
                source_id=source["id"],
                test_result=result,
            )
        assert stale.value.status == 409
    finally:
        gate.set()
        jobs.close()


def test_previous_proof_is_preserved_but_marked_outdated(catalog):
    source = catalog.sources()["items"][0]
    result = run({ROOT + "map.xml": sitemap(), ARTICLE: article()})
    result["source_url"] = source["url"]
    row = catalog.save(
        "endpoint",
        dict(url=ROOT + "map.xml", kind="sitemap"),
        source_id=source["id"],
        test_result=result,
    )
    before = catalog.endpoint(row["id"])
    assert before["test_config_current"]
    catalog.save_model("auto", dict(version=1, parameters=dict(max_depth=1)))
    after = catalog.endpoint(row["id"])
    assert not after["test_config_current"] and after["current_model_version"] == 2
    assert after["evidence"] == before["evidence"]
    assert after["last_test"] == before["last_test"] and after["version"] == before["version"]


@pytest.mark.parametrize(
    ("body", "page", "parameters", "expected"),
    [
        (sitemap(), article(), {}, ["passed", "passed", "passed"]),
        (sitemap(), article("PodcastEpisode"), {}, ["passed", "not_confirmed", "not_confirmed"]),
        (
            sitemap(),
            FetchResult(ARTICLE, 403, b"", None),
            {},
            ["passed", "not_confirmed", "not_confirmed"],
        ),
        (
            sitemap(),
            b"<html><title>Politica</title><body><h1>Politica</h1></body></html>",
            {},
            ["passed", "not_confirmed", "not_confirmed"],
        ),
        (b"<urlset/>", None, {}, ["passed", "not_tested", "not_tested"]),
        (sitemap(), article(), {"article_samples": 0}, ["passed", "not_tested", "not_tested"]),
        (
            FetchResult(ROOT + "map.xml", 403, b"", None),
            None,
            {},
            ["not_confirmed", "not_tested", "not_tested"],
        ),
    ],
)
def test_three_checks_qualify_only_accessible_textual_news(body, page, parameters, expected):
    result = run({ROOT + "map.xml": body, ARTICLE: page}, parameters=parameters)
    assert result["assessment_version"] == 1
    assert [check["status"] for check in result["checks"]] == expected
    assert result["usable_for_news"] is (expected == ["passed"] * 3)
    assert result["article_samples"] == (1 if expected[1] == "passed" else 0)


def test_readable_html_is_not_usable_when_model_is_incompatible():
    result = run({ROOT + "map.xml": feed(), ARTICLE: article()}, "sitemap")
    assert result["checks"][0]["status"] == "passed"
    assert result["xml_valid"] and not result["compatible"]
    assert not result["usable_for_news"] and not result["samples"]


def test_one_textual_article_proves_path_even_with_other_failed_samples():
    second = ROOT + "noticia/bloqueada"
    result = run({ROOT + "map.xml": sitemap([ARTICLE, second]), ARTICLE: article()})
    assert result["usable_for_news"]
    assert result["article_samples"] == result["text_samples"] == 1
    assert len(result["samples"]) == 2
    assert result["samples"][1]["status"] == 404


def test_news_without_useful_body_is_not_usable_for_text_search():
    page = article().replace(b"<p>", b"<span>").replace(b"</p>", b"</span>")
    # Keep an article identity but remove its substantive body.
    start, end = page.index(b"<span>"), page.index(b"</article>")
    page = page[:start] + b"<p>Texto curto.</p>" + page[end:]
    result = run({ROOT + "map.xml": sitemap(), ARTICLE: page})
    assert not result["usable_for_news"]
    assert not result["samples"][0]["text"]
    assert result["checks"][2]["status"] == "not_confirmed"


def test_source_address_change_preserves_but_invalidates_previous_test(catalog):
    source = catalog.sources()["items"][0]
    result = run({ROOT + "map.xml": sitemap(), ARTICLE: article()}, "sitemap")
    result["source_id"], result["source_url"] = source["id"], source["url"]
    row = catalog.save(
        "endpoint",
        dict(url=ROOT + "map.xml", kind="sitemap", model="sitemap"),
        source_id=source["id"],
        test_result=result,
    )
    assert catalog.endpoint(row["id"])["test_config_current"]
    catalog.save("source", dict(version=source["version"], url="https://new.example"), source["id"])
    endpoint = catalog.endpoint(row["id"])
    assert not endpoint["test_config_current"]
    assert endpoint["last_test"]["usable_for_news"]  # Resultado histórico continua intacto.
    assert not catalog.endpoints(source["id"])["items"][0]["test_config_current"]


def test_individual_functional_test_ignores_age_without_changing_search_settings(catalog):
    import time

    model = catalog.model_settings("rss")
    catalog.save_model("rss", dict(version=model["version"], parameters={"lookback_days": 365}))
    source = catalog.sources()["items"][0]
    jobs = TestJobs(
        catalog,
        lambda roots, progress: FakeReader(
            {ROOT + "old-feed": feed(), ARTICLE: article()}, progress
        ),
    )
    try:
        job = jobs.start(
            dict(
                source_id=source["id"],
                url=ROOT + "old-feed",
                kind="feed",
                model="rss",
                bright_data=False,
            )
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = jobs.get(job["id"])
            if status["state"] != "running":
                break
            time.sleep(0.01)
        assert status["state"] == "completed"
        assert status["result"]["usable_for_news"]
        assert status["result"]["parameters"]["lookback_days"] == 0
        assert catalog.model_settings("rss")["parameters"]["lookback_days"] == 365
    finally:
        jobs.close()
