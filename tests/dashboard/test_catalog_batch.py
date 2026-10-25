"""Janela anual, fila finita e aplicação auditada das decisões em dados simulados."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.catalog.batch import ValidationBatch, create_run, status
from buscador_midias.catalog.cadence import estimate_cadence
from buscador_midias.catalog.store import CatalogError, CatalogStore
from buscador_midias.catalog.testing import run_test
from buscador_midias.net.fetcher import FetchResult

ROOT = "https://news.example"
ARTICLE = ROOT + "/noticia/cidade"


def html(date):
    text = (
        "A reportagem ouviu moradores e autoridades sobre os acontecimentos na cidade. "
        "Os documentos apresentados detalham os impactos das medidas anunciadas. "
    )
    return (
        "<html><head><title>Reportagem sobre os acontecimentos na cidade</title>"
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "NewsArticle",
                "headline": "Reportagem sobre os acontecimentos na cidade",
                "datePublished": date,
            }
        )
        + "</script></head><body><article><h1>Reportagem sobre os acontecimentos na cidade</h1>"
        + ("<p>" + text + "</p>") * 15
        + "</article></body></html>"
    ).encode()


class Reader:
    def __init__(self, roots, progress):
        self.requests = 0

    def get(self, url):
        self.requests += 1
        if url == ARTICLE:
            body, mime = html(datetime.now(UTC).isoformat()), "text/html"
        elif url.endswith("good"):
            body, mime = (
                f"<rss><channel><item><link>{ARTICLE}</link></item></channel></rss>".encode(),
                "text/xml",
            )
        elif url.endswith("wrong"):
            body, mime = f"<urlset><url><loc>{ARTICLE}</loc></url></urlset>".encode(), "text/xml"
        else:
            return FetchResult(url, 404, b"", None, url, "text/html")
        return FetchResult(url, 200, body, None, url, mime)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("buscador_midias.catalog.batch.RETRY_SECONDS", 0)
    csv = tmp_path / "sites.csv"
    csv.write_text(
        "region,name,url,country,country_code,language,source_type\n"
        f"América,Fonte,{ROOT},Brasil,BR,Português,portal\n"
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(dict(sources=[dict(key="news.example", name="Fonte")], mechanisms={}))
    )
    return CatalogStore(tmp_path / "catalog.sqlite", snapshot, csv)


def add(store, suffix):
    source = store.sources()["items"][0]
    return store.save(
        "endpoint", dict(url=ROOT + "/" + suffix, kind="feed", model="rss"), source_id=source["id"]
    )


def test_batch_tests_every_link_and_only_then_keeps_proven_links(store):
    good, bad, wrong = [add(store, s) for s in ("good", "bad", "wrong")]
    orphan = store.save("source", dict(name="Sem links", url="https://empty.example"))
    ident = create_run(store)
    batch = ValidationBatch(store, ident, workers=2, factory=Reader)
    with pytest.raises(CatalogError, match="sem decisão"):
        batch.finalize()
    batch.collect()
    assert store.endpoints(good["source_id"])["total"] == 3
    assert status(store, ident)["counts"] == dict(unconfirmed=1, validated=2)
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT attempts FROM catalog_validation_links WHERE link_id=?", (bad["id"],)
            ).fetchone()[0]
            == 3
        )
    batch.finalize()
    assert store.endpoints(good["source_id"])["total"] == 2
    assert store.endpoint(wrong["id"])["model"] == "auto"
    assert store.endpoint(wrong["id"])["kind"] == "sitemap"
    assert not store.source(orphan["id"])["active"]
    assert status(store, ident)["status"] == "completed"
    with store.connection() as db:
        assert db.execute("SELECT deleted_at FROM links WHERE id=?", (bad["id"],)).fetchone()[0]
        assert db.execute("SELECT count(*) FROM link_test_results").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM link_cadence").fetchone()[0] == 3


def test_edits_after_collection_are_not_deleted(store):
    link = add(store, "bad")
    ident = create_run(store)
    batch = ValidationBatch(store, ident, workers=1, factory=Reader)
    batch.collect()
    store.save("endpoint", dict(version=link["version"], notes="Edição manual"), link["id"])
    batch.finalize()
    assert store.endpoint(link["id"])["notes"] == "Edição manual"
    assert status(store, ident)["counts"] == dict(conflict=1)
    assert status(store, ident)["status"] == "completed_with_conflicts"


def test_year_window_rejects_old_article_even_without_xml_date():
    class OldReader(Reader):
        def get(self, url):
            if url == ARTICLE:
                return FetchResult(url, 200, html("2001-01-01T00:00:00Z"), None, url, "text/html")
            return super().get(url)

    config = dict(
        test_id="window",
        url=ROOT + "/good",
        kind="feed",
        model="rss",
        parameters={"lookback_days": 365},
    )
    result = run_test(config, dict(id=1, name="Fonte", url=ROOT), lambda _: None, OldReader)
    assert result["samples"][0]["text"] and not result["samples"][0]["eligible"]
    assert not result["usable_for_news"] and result["text_samples"] == 0


def test_cadence_requires_multiple_distinct_dates():
    now = datetime.now(UTC)
    assert estimate_cadence({"one": now})["status"] == "unknown"
    assert estimate_cadence({"one": now, "two": now, "three": now})["status"] == "unknown"
    result = estimate_cadence({str(i): now - timedelta(hours=i) for i in range(10)})
    assert result["interval_seconds"] == 3600 and result["confidence"] == "low"


def test_third_attempt_uses_detected_sitemap_type_and_remains_bounded(store):
    class AudioReader(Reader):
        def get(self, url):
            response = super().get(url)
            if url == ARTICLE:
                return FetchResult(
                    url,
                    200,
                    response.content.replace(b"NewsArticle", b"PodcastEpisode"),
                    None,
                    url,
                    "text/html",
                )
            return response

    link = add(store, "wrong")
    ident = create_run(store)
    batch = ValidationBatch(store, ident, workers=1, factory=AudioReader)
    batch.collect()
    with store.connection() as db:
        models = [
            r[0]
            for r in db.execute(
                "SELECT model FROM catalog_validation_attempts WHERE run_id=? ORDER BY attempt",
                (ident,),
            )
        ]
    assert models == ["rss", "auto", "sitemap"]
    batch.finalize()
    assert status(store, ident)["counts"] == {"excluded": 1}
    assert store.source(link["source_id"])["active"] == 0


def test_source_count_contract_remains_numeric_for_dashboard(store):
    add(store, "good")
    source = store.sources()["items"][0]
    assert source["endpoints"] == 1
    assert source["rss"] == 1 and source["sitemaps"] == 0


def test_annual_window_moves_with_attempt_instead_of_freezing_at_run_start(store):
    stamp = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    class RecentReader(Reader):
        def get(self, url):
            if url.endswith("good"):
                body = (
                    f"<rss><channel><item><link>{ARTICLE}</link><pubDate>{stamp}</pubDate>"
                    "</item></channel></rss>"
                ).encode()
                return FetchResult(url, 200, body, None, url, "text/xml")
            if url == ARTICLE:
                return FetchResult(url, 200, html(stamp), None, url, "text/html")
            return super().get(url)

    add(store, "good")
    ident = create_run(store)
    batch = ValidationBatch(store, ident, workers=1, factory=RecentReader)
    batch.run["window_until"] = "2001-01-01T00:00:00+00:00"
    batch.collect()
    assert status(store, ident)["counts"] == {"validated": 1}


def test_index_samples_multiple_leaves_so_old_first_child_does_not_condemn_parent():
    stamp = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    class IndexReader(Reader):
        def get(self, url):
            if url.endswith("/index"):
                body = (
                    "<sitemapindex>"
                    + "".join(f"<sitemap><loc>{ROOT}/leaf{i}</loc></sitemap>" for i in range(3))
                    + "</sitemapindex>"
                ).encode()
            elif "/leaf" in url:
                n = url[-1]
                body = f"<urlset><url><loc>{ROOT}/noticia/{n}</loc></url></urlset>".encode()
            else:
                return FetchResult(
                    url,
                    200,
                    html(stamp if url.endswith("2") else "2001-01-01T00:00:00Z"),
                    None,
                    url,
                    "text/html",
                )
            return FetchResult(url, 200, body, None, url, "text/xml")

    result = run_test(
        dict(
            test_id="leaves",
            url=ROOT + "/index",
            model="sitemap_tree",
            kind="sitemap",
            parameters={"lookback_days": 365},
        ),
        dict(id=1, name="Fonte", url=ROOT),
        lambda _: None,
        IndexReader,
    )
    assert result["usable_for_news"] and result["text_samples"] == 1
    assert result["xml_documents"] == 4


def test_sitemap_lastmod_is_only_order_hint_never_publication_date():
    from buscador_midias.catalog.mechanisms.parsing import parse_document

    doc = parse_document(
        f"<urlset><url><loc>{ARTICLE}</loc><lastmod>2026-10-23</lastmod></url></urlset>".encode(),
        ROOT,
    )
    assert doc["dates"] == {}
    assert ARTICLE in doc["hints"]


def test_child_outside_scope_is_a_recorded_fetch_failure_not_a_batch_crash():
    from buscador_midias.catalog.batch_reader import BatchCache, BatchReader
    from buscador_midias.catalog.mechanisms import get_mechanism

    reader = BatchReader(
        [ROOT, ROOT + "/index"], lambda _: None, BatchCache(), get_mechanism("auto").defaults(), 1
    )
    result = reader.get("https://other.example/sitemap.xml")
    assert not result.ok and "fora dos domínios" in result.error
    assert reader.requests == 0


def test_health_uses_recent_proven_sources_when_fixed_controls_fail(store, monkeypatch):
    import buscador_midias.catalog.batch as module

    add(store, "good")
    ident = create_run(store)
    batch = ValidationBatch(store, ident, workers=1, factory=Reader)
    batch.collect()
    batch.factory = None
    visited = []

    class HealthReader:
        def __init__(self, roots, progress, **kwargs):
            visited.append(roots)

        def get(self, url):
            if url == ROOT + "/good":
                return FetchResult(url, 200, b"<rss><channel/></rss>", None, url, "text/xml")
            return FetchResult(url, 503, b"", None, url, "text/html")

    monkeypatch.setattr(module, "TestReader", HealthReader)
    batch.health()
    assert any(ROOT + "/good" in roots for roots in visited)


def test_health_still_pauses_if_every_control_fails(store, monkeypatch):
    import buscador_midias.catalog.batch as module

    ident = create_run(store)
    batch = ValidationBatch(store, ident)

    class OfflineReader:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, url):
            return FetchResult(url, None, b"", "network unavailable")

    monkeypatch.setattr(module, "TestReader", OfflineReader)
    with pytest.raises(OSError, match="Controles de rede indisponíveis"):
        batch.health()
