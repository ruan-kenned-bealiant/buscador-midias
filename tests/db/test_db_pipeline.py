"""As seis etapas do motor real, do feed ao corpus, sobre o novo store."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
from test_db_workstore import FakeReader, rows, story_html

from buscador_midias.analysis.context import ContextVerdict
from buscador_midias.analysis.entity_context import EntityContext
from buscador_midias.corpus.writer import WriteOutcome
from buscador_midias.db.catalog import CatalogRepository
from buscador_midias.db.settings import SettingsRepository
from buscador_midias.db.store import WorkStore
from buscador_midias.domain.url import url_hash
from buscador_midias.evidence.storage import LocalEvidenceStore
from buscador_midias.ingestion.artifacts import Artifacts
from buscador_midias.ingestion.engine import Engine, RetryableError
from buscador_midias.net.fetcher import FetchResult

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

ROOT = "https://news.example/"
FEED = ROOT + "feed"
STORY = ROOT + "noticia/hoje-na-cidade"

CORPUS = """
CREATE TABLE midias_ia_articles (
 id bigint PRIMARY KEY, url_hash text, evidencia_status text, evidencia_path text, body text,
 ai_model text, ai_verdict text, ai_reason text, ai_summary text, resumo text, ai_score real,
 content_category text, source_country text
);
CREATE TABLE midias_ia_article_entities (
 article_id bigint, name text, entity_type text, role text
);
"""


class Analyzer:
    def analyze_entities(self, item: Any, entities: Any) -> tuple[EntityContext, ...]:
        return tuple(
            EntityContext(
                e["name"],
                e["type"],
                e["role"],
                False,
                0.9,
                "A pessoa é citada, sem fato adverso atribuído a ela.",
                "",
                e["name"],
            )
            for e in entities
        )

    def analyze(self, items: Any) -> list[ContextVerdict]:
        return [
            ContextVerdict(
                1,
                True,
                False,
                (),
                ({"name": "João Silva", "type": "PF", "role": "citado"},),
                "",
                "Resumo da reportagem.",
                0.9,
                "text",
            )
        ]


class Writer:
    saved: list[Any] = []

    def source_countries(self) -> dict[str, str]:
        return {"news.example": "BR"}

    def write(self, articles: list[Any]) -> list[WriteOutcome]:
        Writer.saved.extend(articles)
        return [WriteOutcome(articles[0].url_hash, 123, True, False, 1, 1)]


@pytest.fixture
def world(schema_name: str, tmp_path: Path) -> dict[str, Any]:
    Writer.saved = []
    store = WorkStore(schema_name)
    store.install()
    with store.connection() as db:
        db.execute(CORPUS)
    catalog = CatalogRepository(schema_name)
    site = catalog.save("source", dict(name="Notícias", url="https://news.example"))
    catalog.save("endpoint", dict(url=FEED, kind="feed", model="rss"), source_id=site["id"])
    settings = SettingsRepository(schema_name)
    settings.configure_search(
        dict(
            version=settings.search()["version"], config=dict(enabled=True, daily_routes_only=False)
        )
    )
    now = datetime.now(UTC)
    html = story_html(now).replace(
        b"A reportagem ouviu", "João Silva participou. A reportagem ouviu".encode()
    )
    data: dict[str, Any] = {
        FEED: (
            f"<rss><channel><item><link>{STORY}</link>"
            f"<pubDate>{format_datetime(now)}</pubDate></item></channel></rss>"
        ).encode(),
        STORY: FetchResult(STORY, 200, html, None, STORY, "text/html"),
    }
    engine = Engine(
        store,
        artifacts=Artifacts(tmp_path / "work"),
        reader_factory=lambda roots, progress: FakeReader(data, progress),
        analyzer_factory=Analyzer,
        evidence_store=LocalEvidenceStore(tmp_path / "evidence"),
        writer_factory=Writer,
        corpus_schema=schema_name,
    )
    return dict(store=store, engine=engine, work=tmp_path / "work", data=data)


def waiting(store: WorkStore, stage: str) -> list[Any]:
    if stage == "discovery":
        return [
            r["id"] for r in rows(store, "SELECT id FROM bm_discovery_tasks WHERE state='pending'")
        ]
    return [
        r["hash"]
        for r in rows(
            store, "SELECT hash FROM bm_articles WHERE stage=%s AND state='pending'", stage
        )
    ]


def step(world: dict[str, Any], stage: str) -> dict[str, Any]:
    store, engine = world["store"], world["engine"]
    jobs = store.claim_many(stage, waiting(store, stage), "w1")
    assert len(jobs) == 1, stage
    outputs = engine.dedup_many(jobs) if stage == "dedup" else [engine.execute(jobs[0])]
    store.finish_many(jobs, outputs)
    return outputs[0]


def test_a_story_goes_from_the_feed_to_the_corpus_through_all_six_stages(world):
    store = world["store"]
    store.create_run({})
    outputs = {stage: step(world, stage) for stage in ("discovery", "dedup", "fetch")}
    assert outputs["dedup"]["next"] == "fetch" and outputs["fetch"]["next"] == "context"
    for stage in ("context", "evidence", "persist"):
        outputs[stage] = step(world, stage)
    [article] = rows(store, "SELECT * FROM bm_articles")
    assert (article["state"], article["outcome"], article["corpus_article_id"]) == (
        "done",
        "persisted",
        123,
    )
    assert [h["stage"] for h in article["history"]] == [
        "dedup",
        "fetch",
        "context",
        "evidence",
        "persist",
    ]
    assert not (world["work"] / (article["hash"] + ".html.gz")).exists()
    saved = Writer.saved[0]
    assert saved.entities[0].name == "João Silva" and saved.evidence.status == "captured"
    assert store.maintain()["runs_closed"] == 1
    [run] = rows(store, "SELECT status,finished_at FROM bm_runs")
    assert run["status"] == "completed" and run["finished_at"] is not None


def test_cost_and_metrics_of_every_stage_are_kept_on_the_article(world):
    store = world["store"]
    store.create_run({})
    for stage in ("discovery", "dedup", "fetch", "context", "evidence", "persist"):
        step(world, stage)
    [article] = rows(store, "SELECT cost FROM bm_articles")
    assert {"dedup", "fetch"} <= set(article["cost"])
    assert article["cost"]["dedup"]["dedup_batch_size"] == 1


def test_a_story_already_complete_in_the_corpus_is_known_and_never_analysed(world):
    store = world["store"]
    with store.connection() as db:
        db.execute(
            "INSERT INTO midias_ia_articles VALUES(55,%s,'captured','midias-ia/e.html','texto',"
            "'modelo','ok','r','s','r',0.9,'news','BR')",
            (url_hash(STORY),),
        )
        db.execute("INSERT INTO midias_ia_article_entities VALUES(55,'João Silva','PER','citado')")
    store.create_run({})
    step(world, "discovery")
    output = step(world, "dedup")
    assert output["status"] == "known"
    [article] = rows(store, "SELECT * FROM bm_articles")
    assert (article["state"], article["outcome"], article["corpus_article_id"]) == (
        "done",
        "known",
        55,
    )
    assert waiting(store, "fetch") == []


# --- notícia recusada com o transporte do link ---------------------------------------------------


def refusing_direct(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, *, only: str | None):
    """Leitores por transporte: a notícia dá 403 no ``direct`` e abre em ``only`` (ou em nenhum)."""
    seen: list[str] = []

    @contextmanager
    def by_transport(
        job: dict[str, Any],
        roots: list[str],
        transport: str | None = None,
        gap: float | None = None,
    ):
        name = transport or job["snapshot"].get("access_transport", "direct")
        seen.append(name)
        data = dict(world["data"])
        if name != only:
            data[STORY] = FetchResult(STORY, 403, b"", None, STORY, "text/html")
        reader = FakeReader(data, None)
        reader.transport = name
        yield reader

    monkeypatch.setattr(world["engine"], "reader", by_transport)
    return seen


def test_a_story_refused_with_the_link_transport_is_read_with_another_one(world, monkeypatch):
    seen = refusing_direct(world, monkeypatch, only="curl_chrome_h2")
    world["store"].create_run({})

    outputs = {stage: step(world, stage) for stage in ("discovery", "dedup", "fetch")}

    assert outputs["fetch"]["next"] == "context"
    metrics = outputs["fetch"]["metrics"]
    assert metrics["access_transport"] == "curl_chrome_h2"
    assert metrics["transport_attempts"] == ["direct", "curl_chrome_h2"]
    assert metrics["transport_fallback"] == "curl_chrome_h2"
    assert seen[-2:] == ["direct", "curl_chrome_h2"]
    # Só números sobrevivem no custo da etapa: é por eles que se mede o ganho em produção.
    [article] = rows(world["store"], "SELECT cost FROM bm_articles")
    assert article["cost"]["fetch"]["transport_fallbacks"] == 1
    assert article["cost"]["fetch"]["transport_extra_requests"] == 1


def test_a_story_refused_by_every_transport_still_fails_as_blocked_with_the_attempts(
    world, monkeypatch
):
    seen = refusing_direct(world, monkeypatch, only=None)
    store = world["store"]
    store.create_run({})
    for stage in ("discovery", "dedup"):
        step(world, stage)
    [job] = store.claim_many("fetch", waiting(store, "fetch"), "w1")

    with pytest.raises(RetryableError) as failed:
        world["engine"].execute(job)

    assert "403" in str(failed.value) and failed.value.retryable is False
    assert failed.value.metrics["access_transport"] == "direct"
    assert failed.value.metrics["transport_attempts"] == [
        "direct",
        "curl_chrome_h2",
        "curl_chrome_h1",
        "curl_firefox_h2",
    ]
    assert seen[-4:] == ["direct", "curl_chrome_h2", "curl_chrome_h1", "curl_firefox_h2"]


def test_the_story_reader_asks_for_a_wider_gap_between_requests_to_the_same_site(
    world, monkeypatch
):
    """Feeds seguem com 300 ms; as notícias (onde o WAF dos sites reage) pedem 1,5 s."""
    seen: list[dict[str, Any]] = []

    def fake(roots, progress, **kwargs):
        seen.append(kwargs)
        return FakeReader(world["data"], progress)

    monkeypatch.setattr("buscador_midias.ingestion.engine.make_reader", fake)
    engine = world["engine"]
    engine.reader_factory = None
    job = dict(snapshot=dict(access_transport="direct"))

    with engine.reader(job, ["https://news.example"]):
        pass
    with engine.reader(job, ["https://news.example"], gap=1.5):
        pass

    assert "host_gap" not in seen[0]
    assert seen[1]["host_gap"] == 1.5
