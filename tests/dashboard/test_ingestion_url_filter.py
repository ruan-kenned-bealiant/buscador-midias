# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Exclusão estrutural por URL: página de índice e data do caminho anterior à janela."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_catalog_testing import FakeReader
from test_ingestion import ingest, next_job  # noqa: F401

from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion.engine import Engine
from buscador_midias.ingestion.flow import index_page, path_date_outside, url_outside

# 27/10 00:00 em São Paulo (UTC-3): o início da janela de uma rodada diária.
SINCE = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)
SAO_PAULO = "America/Sao_Paulo"

needs_postgres = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.mark.parametrize(
    ("url", "day"),
    [
        (
            "https://g1.globo.com/pi/piaui/eleicoes/2026/noticia/2026/10/26/apuracao.ghtml",
            "2026-10-26",
        ),
        ("https://exemplo.example/2026/10/26/materia", "2026-10-26"),
        ("https://exemplo.example/2026/10/26", "2026-10-26"),
        ("https://exemplo.example/politica/2026/10/01/materia-antiga.html", "2026-10-01"),
    ],
)
def test_path_date_before_the_window_day_is_outside(url, day):
    found = path_date_outside(url, SINCE, SAO_PAULO)
    assert found["rule"] == "path_date_before_window"
    assert found["path_date"] == day


def test_path_date_of_the_window_day_is_kept():
    assert path_date_outside("https://exemplo.example/2026/10/27/materia", SINCE, SAO_PAULO) is None


def test_path_date_after_the_window_start_is_never_a_reason_to_exclude():
    assert path_date_outside("https://exemplo.example/2026/10/28/materia", SINCE, SAO_PAULO) is None


def test_the_day_is_judged_in_the_run_timezone_not_in_utc():
    # 26/10 inteiro em São Paulo termina às 03:00 UTC de 27/10, exatamente o início da janela.
    just_before = SINCE - timedelta(seconds=1)
    assert path_date_outside("https://exemplo.example/2026/10/26/a", SINCE, SAO_PAULO)
    assert path_date_outside("https://exemplo.example/2026/10/26/a", just_before, SAO_PAULO) is None


def test_a_window_starting_midday_keeps_the_same_day():
    midday = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
    assert path_date_outside("https://exemplo.example/2026/10/27/a", midday, SAO_PAULO) is None
    assert path_date_outside("https://exemplo.example/2026/10/26/a", midday, SAO_PAULO)


def test_other_timezone_moves_the_day_boundary():
    los_angeles = "America/Los_Angeles"
    start = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)  # 00:00 PDT
    assert path_date_outside("https://exemplo.example/2026/10/26/a", start, los_angeles)
    assert path_date_outside("https://exemplo.example/2026/10/27/a", start, los_angeles) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://exemplo.example/materia-sem-data",
        "https://exemplo.example/2026/13/45/data-impossivel",
        "https://exemplo.example/2026/10/261234/numero-colado",
        "https://exemplo.example/busca?d=/2026/10/20/",
        "https://exemplo.example/2026/10/",
        "",
    ],
)
def test_urls_without_a_trustworthy_path_date_are_never_excluded(url):
    assert path_date_outside(url, SINCE, SAO_PAULO) is None


def test_invalid_timezone_never_excludes():
    assert path_date_outside("https://exemplo.example/2026/10/01/a", SINCE, "Nao/Existe") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.scmp.com/topics/100-top-picks",
        "https://www.scmp.com/author/aadish-kumar",
        "https://www.scmp.com/taxonomy/term/507760",
        "https://www.rtl.lu/authors/athenais-erodiades",
        "https://exemplo.example/tag/eleicoes",
        "https://exemplo.example/tags/eleicoes?pagina=2",
        "https://exemplo.example/Topic/economia",
        "https://exemplo.example/author",
    ],
)
def test_index_pages_are_recognised_by_the_first_path_segment(url):
    assert index_page(url)["rule"] == "index_page"


@pytest.mark.parametrize(
    "url",
    [
        # Artigo real cujo caminho apenas contém a palavra mais adiante.
        "https://www.scmp.com/native/business/topics/domestic-global-success/article/3371711/x",
        "https://www.bbc.com/news/topics/c123",
        "https://exemplo.example/topicsx/materia",
        "https://exemplo.example/autor-premiado/materia",
        "https://exemplo.example/materia?tag=eleicoes",
        "https://exemplo.example/",
        "",
    ],
)
def test_pages_that_only_mention_the_word_are_not_index_pages(url):
    assert index_page(url) is None


def test_url_outside_reports_the_index_rule_before_the_date_rule():
    found = url_outside("https://exemplo.example/tag/2026/10/01/x", SINCE, SAO_PAULO)
    assert found["rule"] == "index_page"


def test_url_outside_can_skip_the_date_rule_when_publication_is_explicit():
    old = "https://exemplo.example/2026/10/01/x"
    assert url_outside(old, SINCE, SAO_PAULO)["rule"] == "path_date_before_window"
    assert url_outside(old, SINCE, SAO_PAULO, explicit_date=True) is None
    assert url_outside("https://exemplo.example/tag/x", SINCE, SAO_PAULO, explicit_date=True)


def sitemap_body(*entries):
    return (
        "<urlset>" + "".join(f"<url><loc>{url}</loc></url>" for url in entries) + "</urlset>"
    ).encode()


def discover(ingest, mode):
    ingest.configure(
        dict(
            version=ingest.settings()["version"],
            config=dict(window_mode="today", url_filter=mode, lastmod_filter="off"),
        )
    )
    ingest.create_run({})
    job = next_job(ingest, "discovery")
    job["snapshot"].update(
        model="sitemap", kind="sitemap", parameters=get_mechanism("sitemap").defaults()
    )
    today = job["run"]["since"].astimezone(ZoneInfo(SAO_PAULO))
    old, new = (today - timedelta(days=1)), today
    body = sitemap_body(
        "https://jose.example/topics/economia",
        "https://jose.example/author/maria",
        f"https://jose.example/{old:%Y/%m/%d}/velha",
        f"https://jose.example/{new:%Y/%m/%d}/nova",
        "https://jose.example/sem-data",
    )
    engine = Engine(
        ingest,
        reader_factory=lambda roots, progress: FakeReader({job["payload"]["url"]: body}, progress),
    )
    return engine.discovery(job)


def names(result):
    return {c["url"].rsplit("/", 1)[1]: c for c in result["candidates"]}


@needs_postgres
def test_enforce_drops_index_pages_and_old_path_dates_without_creating_tasks(ingest):
    result = discover(ingest, "enforce")
    assert set(names(result)) == {"nova", "sem-data"}
    assert result["metrics"]["url_excluded_urls"] == 3


@needs_postgres
def test_shadow_keeps_every_url_and_records_what_would_have_been_excluded(ingest):
    result = discover(ingest, "shadow")
    found = names(result)
    assert set(found) == {"economia", "maria", "velha", "nova", "sem-data"}
    assert found["economia"]["shadow_url_exclusion"]["rule"] == "index_page"
    assert found["maria"]["shadow_url_exclusion"]["rule"] == "index_page"
    assert found["velha"]["shadow_url_exclusion"]["rule"] == "path_date_before_window"
    assert "shadow_url_exclusion" not in found["nova"]
    assert "shadow_url_exclusion" not in found["sem-data"]
    assert result["metrics"]["url_excluded_urls"] == 0


@needs_postgres
def test_off_ignores_the_url_shape_completely(ingest):
    result = discover(ingest, "off")
    assert len(result["candidates"]) == 5
    assert not any("shadow_url_exclusion" in c for c in result["candidates"])


@needs_postgres
def test_explicit_publication_date_beats_the_path_date_but_not_the_index_rule(ingest):
    ingest.configure(
        dict(
            version=ingest.settings()["version"],
            config=dict(window_mode="today", url_filter="enforce", lastmod_filter="off"),
        )
    )
    ingest.create_run({})
    job = next_job(ingest, "discovery")
    job["snapshot"].update(
        model="news_sitemap", kind="sitemap", parameters=get_mechanism("news_sitemap").defaults()
    )
    since = job["run"]["since"]
    published = (since + timedelta(hours=2)).isoformat()

    def item(url):
        return (
            f"<url><loc>{url}</loc><news:news><news:publication_date>{published}"
            "</news:publication_date><news:title>t</news:title></news:news></url>"
        )

    body = (
        '<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        + item("https://jose.example/2026/01/01/republicada")
        + item("https://jose.example/tag/republicada")
        + "</urlset>"
    ).encode()
    engine = Engine(
        ingest,
        reader_factory=lambda roots, progress: FakeReader({job["payload"]["url"]: body}, progress),
    )
    result = engine.discovery(job)
    assert [c["url"] for c in result["candidates"]] == [
        "https://jose.example/2026/01/01/republicada"
    ]


@needs_postgres
@pytest.mark.parametrize("value", ["agressivo", True, None, 1])
def test_url_filter_setting_is_validated(ingest, value):
    with pytest.raises(CatalogError):
        ingest.configure(dict(version=ingest.settings()["version"], config=dict(url_filter=value)))


@needs_postgres
def test_url_filter_defaults_to_shadow_and_accepts_every_mode(ingest):
    assert ingest.settings()["config"]["url_filter"] == "shadow"
    for mode in ("off", "enforce", "shadow"):
        ingest.configure(dict(version=ingest.settings()["version"], config=dict(url_filter=mode)))
        assert ingest.settings()["config"]["url_filter"] == mode
