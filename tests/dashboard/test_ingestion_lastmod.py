# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""``lastmod`` do sitemap como limite superior da publicação: só exclui, nunca data."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_catalog_testing import FakeReader
from test_ingestion import ingest, next_job  # noqa: F401

from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion.engine import Engine
from buscador_midias.ingestion.flow import lastmod_outside

SINCE = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)

needs_postgres = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def stamp(delta: timedelta) -> str:
    return (SINCE + delta).isoformat()


@pytest.mark.parametrize(
    "lastmod",
    [
        stamp(timedelta(days=-3)),
        stamp(timedelta(days=-1)),
        "2026-10-24T00:00:00+00:00",
        "2026-10-24T00:00:00",
    ],
)
def test_lastmod_well_before_the_window_is_excluded(lastmod):
    found = lastmod_outside(lastmod, SINCE)
    assert found and found["rule"] == "lastmod_before_window" and found["lastmod"] == lastmod


@pytest.mark.parametrize(
    "lastmod",
    [
        None,
        "",
        "não é data",
        stamp(timedelta(hours=1)),
        SINCE.isoformat(),
        # Margem de 12 h cobre fuso mal rotulado: o instante real pode ser posterior.
        stamp(timedelta(hours=-6)),
        # Data pura vale o dia inteiro: 25/10 ainda pode ser 26/10 em outro fuso.
        "2026-10-25T00:00:00+00:00",
        "2026-10-26T00:00:00+00:00",
        "2026-10-27T00:00:00+00:00",
    ],
)
def test_lastmod_that_may_belong_to_the_window_is_never_excluded(lastmod):
    assert lastmod_outside(lastmod, SINCE) is None


def test_lastmod_after_the_window_end_is_not_a_reason_to_exclude():
    # Modificado depois: nada se sabe sobre quando foi publicado.
    assert lastmod_outside(stamp(timedelta(days=30)), SINCE) is None


def sitemap_body(*entries):
    return (
        "<urlset>"
        + "".join(
            f"<url><loc>{url}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</url>"
            for url, mod in entries
        )
        + "</urlset>"
    ).encode()


def discover(ingest, mode):
    ingest.configure(
        dict(
            version=ingest.settings()["version"],
            config=dict(window_mode="today", lastmod_filter=mode),
        )
    )
    ingest.create_run({})
    job = next_job(ingest, "discovery")
    job["snapshot"].update(
        model="sitemap", kind="sitemap", parameters=get_mechanism("sitemap").defaults()
    )
    since = job["run"]["since"]
    body = sitemap_body(
        ("https://jose.example/velha", (since - timedelta(days=9)).isoformat()),
        ("https://jose.example/nova", (since + timedelta(hours=2)).isoformat()),
        ("https://jose.example/sem-data", None),
    )
    engine = Engine(
        ingest,
        reader_factory=lambda roots, progress: FakeReader({job["payload"]["url"]: body}, progress),
    )
    return engine.discovery(job)


def urls(result):
    return {c["url"].rsplit("/", 1)[1]: c for c in result["candidates"]}


@needs_postgres
def test_enforce_drops_urls_modified_before_the_window_without_creating_tasks(ingest):
    result = discover(ingest, "enforce")
    assert set(urls(result)) == {"nova", "sem-data"}
    assert result["metrics"]["lastmod_excluded_urls"] == 1


@needs_postgres
def test_shadow_keeps_every_url_and_records_what_would_have_been_excluded(ingest):
    result = discover(ingest, "shadow")
    found = urls(result)
    assert set(found) == {"velha", "nova", "sem-data"}
    assert found["velha"]["shadow_lastmod_exclusion"]["rule"] == "lastmod_before_window"
    assert "shadow_lastmod_exclusion" not in found["nova"]
    assert "shadow_lastmod_exclusion" not in found["sem-data"]
    assert result["metrics"]["lastmod_excluded_urls"] == 0


@needs_postgres
def test_off_ignores_lastmod_completely(ingest):
    result = discover(ingest, "off")
    assert set(urls(result)) == {"velha", "nova", "sem-data"}
    assert not any("shadow_lastmod_exclusion" in c for c in result["candidates"])


@needs_postgres
def test_explicit_publication_date_still_decides_before_lastmod(ingest):
    # Publicação explícita (news sitemap) segue a regra antiga; lastmod não a substitui.
    ingest.configure(
        dict(
            version=ingest.settings()["version"],
            config=dict(window_mode="today", lastmod_filter="enforce"),
        )
    )
    ingest.create_run({})
    job = next_job(ingest, "discovery")
    job["snapshot"].update(
        model="news_sitemap", kind="sitemap", parameters=get_mechanism("news_sitemap").defaults()
    )
    since = job["run"]["since"]
    body = (
        '<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        "<url><loc>https://jose.example/noticia</loc>"
        f"<lastmod>{(since - timedelta(days=9)).isoformat()}</lastmod>"
        f"<news:news><news:publication_date>{(since + timedelta(hours=2)).isoformat()}"
        "</news:publication_date><news:title>t</news:title></news:news></url></urlset>"
    ).encode()
    engine = Engine(
        ingest,
        reader_factory=lambda roots, progress: FakeReader({job["payload"]["url"]: body}, progress),
    )
    result = engine.discovery(job)
    assert [c["url"] for c in result["candidates"]] == ["https://jose.example/noticia"]


@needs_postgres
@pytest.mark.parametrize("value", ["agressivo", True, None, 1])
def test_lastmod_filter_setting_is_validated(ingest, value):
    with pytest.raises(CatalogError):
        ingest.configure(
            dict(version=ingest.settings()["version"], config=dict(lastmod_filter=value))
        )


@needs_postgres
def test_lastmod_filter_defaults_to_shadow_and_accepts_every_mode(ingest):
    assert ingest.settings()["config"]["lastmod_filter"] == "shadow"
    for mode in ("off", "enforce", "shadow"):
        ingest.configure(
            dict(version=ingest.settings()["version"], config=dict(lastmod_filter=mode))
        )
        assert ingest.settings()["config"]["lastmod_filter"] == mode
