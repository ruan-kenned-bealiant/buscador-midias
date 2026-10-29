"""Barreiras de conteúdo rotineiro no motor: URL, título e categoria da análise de contexto."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
from test_db_pipeline import CORPUS, Analyzer, Writer
from test_db_workstore import FakeReader, rows, story_html

from buscador_midias.db.catalog import CatalogRepository
from buscador_midias.db.settings import SettingsRepository
from buscador_midias.db.store import WorkStore
from buscador_midias.evidence.storage import LocalEvidenceStore
from buscador_midias.ingestion.artifacts import Artifacts
from buscador_midias.ingestion.engine import Engine
from buscador_midias.net.fetcher import FetchResult

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)
ROOT = "https://news.example/"
FEED = ROOT + "feed"
TITLE = "Reportagem sobre os acontecimentos na cidade"
HOROSCOPE = "Horóscopo do dia: previsão para os 12 signos"


def classified(category: str | None = None, finding: bool = False) -> type:
    class Classified(Analyzer):
        def analyze(self, items: Any) -> list[Any]:
            verdict = super().analyze(items)[0]
            return [replace(verdict, content_category=category, finding=finding)]

    return Classified


def build(
    schema: str,
    tmp_path: Path,
    *,
    path: str = "noticia/hoje-na-cidade",
    title: str | None = None,
    analyzer: type = Analyzer,
    **config: Any,
) -> dict[str, Any]:
    """Um feed com uma notícia em ``path``; ``config`` entra na configuração da rodada."""
    Writer.saved = []
    store = WorkStore(schema)
    store.install()
    with store.connection() as db:
        db.execute(CORPUS)
    catalog = CatalogRepository(schema)
    site = catalog.save("source", dict(name="Notícias", url="https://news.example"))
    catalog.save("endpoint", dict(url=FEED, kind="feed", model="rss"), source_id=site["id"])
    settings = SettingsRepository(schema)
    settings.configure_search(
        dict(
            version=settings.search()["version"],
            config=dict(enabled=True, daily_routes_only=False, **config),
        )
    )
    now = datetime.now(UTC)
    html = story_html(now).replace(
        b"A reportagem ouviu", "João Silva participou. A reportagem ouviu".encode()
    )
    if title:
        html = html.replace(TITLE.encode(), title.encode())
    story = ROOT + path
    requested: list[str] = []

    class Reader(FakeReader):
        def get(self, url: str, **kwargs: Any) -> FetchResult:
            requested.append(url)
            return super().get(url, **kwargs)

    data: dict[str, Any] = {
        FEED: (
            f"<rss><channel><item><link>{story}</link>"
            f"<pubDate>{format_datetime(now)}</pubDate></item></channel></rss>"
        ).encode(),
        story: FetchResult(story, 200, html, None, story, "text/html"),
    }
    engine = Engine(
        store,
        artifacts=Artifacts(tmp_path / "work"),
        reader_factory=lambda roots, progress: Reader(data, progress),
        analyzer_factory=analyzer,
        evidence_store=LocalEvidenceStore(tmp_path / "evidence"),
        writer_factory=Writer,
        corpus_schema=schema,
    )
    store.create_run({})
    return dict(
        store=store, engine=engine, work=tmp_path / "work", story=story, requested=requested
    )


def run(world: dict[str, Any], *stages: str) -> dict[str, Any]:
    store, engine = world["store"], world["engine"]
    for stage in stages:
        ids = (
            [
                r["id"]
                for r in rows(store, "SELECT id FROM bm_discovery_tasks WHERE state='pending'")
            ]
            if stage == "discovery"
            else [
                r["hash"]
                for r in rows(
                    store, "SELECT hash FROM bm_articles WHERE stage=%s AND state='pending'", stage
                )
            ]
        )
        jobs = store.claim_many(stage, ids, "w1")
        outputs = engine.dedup_many(jobs) if stage == "dedup" else [engine.execute(j) for j in jobs]
        store.finish_many(jobs, outputs)
    [article] = rows(store, "SELECT * FROM bm_articles")
    return article


def waiting(world: dict[str, Any], stage: str) -> int:
    return len(
        rows(world["store"], "SELECT 1 FROM bm_articles WHERE stage=%s AND state='pending'", stage)
    )


def test_the_context_barrier_stops_a_routine_category_before_the_evidence(schema_name, tmp_path):
    world = build(schema_name, tmp_path, analyzer=classified("sports"))
    article = run(world, "discovery", "dedup", "fetch", "context")
    assert (article["state"], article["outcome"]) == ("done", "excluded_content")
    assert article["payload"]["exclusion"] == dict(
        category="sports", rule="ai_category", stage="ai"
    )
    assert article["payload"]["analysis"]["content_category"] == "sports"
    assert not article["payload"].get("body") and waiting(world, "evidence") == 0
    assert not (world["work"] / (article["hash"] + ".html.gz")).exists()


def test_a_routine_category_with_an_adverse_finding_is_kept(schema_name, tmp_path):
    world = build(schema_name, tmp_path, analyzer=classified("sports", True))
    article = run(world, "discovery", "dedup", "fetch", "context")
    assert article["outcome"] != "excluded_content" and waiting(world, "evidence") == 1


def test_the_barrier_can_be_disabled_with_an_empty_list(schema_name, tmp_path):
    world = build(
        schema_name, tmp_path, analyzer=classified("sports"), blocked_content_categories=[]
    )
    run(world, "discovery", "dedup", "fetch", "context")
    assert waiting(world, "evidence") == 1


def test_an_enforced_url_rule_skips_the_download(schema_name, tmp_path):
    world = build(schema_name, tmp_path, path="horoscopo/libra", content_filter="enforce")
    article = run(world, "discovery", "dedup", "fetch")
    assert article["outcome"] == "excluded_content"
    assert article["payload"]["exclusion"]["category"] == "horoscope"
    assert article["payload"]["exclusion"]["stage"] == "url"
    assert world["story"] not in world["requested"] and waiting(world, "context") == 0


def test_an_enforced_title_rule_stops_after_extraction_and_keeps_no_artifact(schema_name, tmp_path):
    world = build(schema_name, tmp_path, title=HOROSCOPE, content_filter="enforce")
    article = run(world, "discovery", "dedup", "fetch")
    assert article["outcome"] == "excluded_content"
    assert article["payload"]["exclusion"]["stage"] == "title"
    assert not article["payload"].get("body") and waiting(world, "context") == 0
    assert not (world["work"] / (article["hash"] + ".html.gz")).exists()


def test_shadow_mode_records_the_rule_but_keeps_processing(schema_name, tmp_path):
    world = build(schema_name, tmp_path, title=HOROSCOPE, content_filter="shadow")
    article = run(world, "discovery", "dedup", "fetch")
    assert article["outcome"] != "excluded_content"
    assert article["payload"]["shadow_exclusion"]["category"] == "horoscope"
    assert waiting(world, "context") == 1


def test_off_mode_ignores_the_rules(schema_name, tmp_path):
    world = build(
        schema_name, tmp_path, path="horoscopo/libra", title=HOROSCOPE, content_filter="off"
    )
    article = run(world, "discovery", "dedup", "fetch")
    assert "shadow_exclusion" not in article["payload"] and "exclusion" not in article["payload"]
    assert waiting(world, "context") == 1


def test_an_adverse_title_is_never_excluded_even_when_enforced(schema_name, tmp_path):
    world = build(
        schema_name,
        tmp_path,
        title="Horóscopo: astrólogo é preso por estelionato",
        content_filter="enforce",
    )
    article = run(world, "discovery", "dedup", "fetch")
    assert article["outcome"] != "excluded_content" and waiting(world, "context") == 1
