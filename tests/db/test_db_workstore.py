"""O motor real (``Engine``) rodando sobre o novo store: o formato das tarefas é o mesmo."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest

from buscador_midias.db.catalog import CatalogRepository
from buscador_midias.db.settings import SettingsRepository
from buscador_midias.db.store import WorkStore
from buscador_midias.ingestion.artifacts import Artifacts
from buscador_midias.ingestion.engine import Engine, RetryableError
from buscador_midias.net.fetcher import FetchResult

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

ROOT = "https://news.example/"
FEED = ROOT + "feed"
STORY = ROOT + "noticia/hoje-na-cidade"


def story_html(published: datetime) -> bytes:
    data = {
        "@type": "NewsArticle",
        "headline": "Reportagem sobre os acontecimentos na cidade",
        "datePublished": published.isoformat(),
    }
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


class FakeReader:
    def __init__(self, data: dict[str, Any], progress: Any) -> None:
        self.data, self.requests = data, 0

    def get(self, url: str, **_: Any) -> FetchResult:
        self.requests += 1
        response = self.data.get(url)
        if isinstance(response, FetchResult):
            return response
        return FetchResult(
            url,
            200 if response is not None else 404,
            response or b"",
            None,
            final_url=url,
            content_type="text/html" if url == STORY else "text/xml",
        )


@pytest.fixture
def world(schema_name: str, tmp_path: Path) -> dict[str, Any]:
    store = WorkStore(schema_name)
    store.install()
    catalog = CatalogRepository(schema_name)
    site = catalog.save("source", dict(name="Notícias", url="https://news.example"))
    link = catalog.save("endpoint", dict(url=FEED, kind="feed", model="rss"), source_id=site["id"])
    settings = SettingsRepository(schema_name)
    settings.configure_search(
        dict(
            version=settings.search()["version"], config=dict(enabled=True, daily_routes_only=False)
        )
    )
    now = datetime.now(UTC)
    feed = (
        f"<rss><channel><item><link>{STORY}</link>"
        f"<pubDate>{format_datetime(now)}</pubDate></item></channel></rss>"
    ).encode()
    data: dict[str, Any] = {FEED: feed, STORY: story_html(now)}
    engine = Engine(
        store,
        artifacts=Artifacts(tmp_path / "work"),
        reader_factory=lambda roots, progress: FakeReader(data, progress),
    )
    return dict(store=store, engine=engine, site=site, link=link, data=data, now=now)


def rows(store: WorkStore, query: str, *args: Any) -> list[dict[str, Any]]:
    with store.connection() as db:
        return db.execute(query, args).fetchall()


def run_discovery(world: dict[str, Any]) -> dict[str, Any]:
    store, engine = world["store"], world["engine"]
    run = store.create_run({})
    ids = [r["id"] for r in rows(store, "SELECT id FROM bm_discovery_tasks")]
    job = store.claim_many("discovery", ids, "w1")[0]
    assert store.finish(job, engine.execute(job))
    return run


def test_discovery_with_the_real_engine_admits_the_story_and_closes_the_read(world):
    store = world["store"]
    run = run_discovery(world)
    assert run["links"] == 1 and run["status"] == "running"
    [article] = rows(store, "SELECT * FROM bm_articles")
    assert article["url"] == STORY and article["stage"] == "dedup" and article["state"] == "pending"
    assert article["site_id"] == world["site"]["id"] and article["document_url"] == FEED
    [read] = rows(store, "SELECT * FROM bm_run_links")
    assert (read["state"], read["discovery_done"], read["listed"], read["new_articles"]) == (
        "completed",
        True,
        1,
        1,
    )
    [task] = rows(store, "SELECT * FROM bm_discovery_tasks")
    assert task["state"] == "done" and task["outcome"] == "urls_encontradas"


def test_the_engine_sees_the_same_job_shape_for_an_article(world):
    store = world["store"]
    run_discovery(world)
    [article] = rows(store, "SELECT hash FROM bm_articles")
    job = store.claim_many("dedup", [article["hash"]], "w1")[0]
    snapshot = job["snapshot"]
    assert snapshot["source"]["name"] == "Notícias" and snapshot["url"] == FEED
    assert snapshot["model"] == "rss" and snapshot["access_transport"] == "direct"
    assert job["article"]["url"] == STORY and job["run"]["config"]["enabled"] is True
    assert job["id"] == job["hash"] == article["hash"]


def test_fetching_a_story_advances_it_to_the_context_stage(world):
    store, engine = world["store"], world["engine"]
    run_discovery(world)
    [article] = rows(store, "SELECT hash FROM bm_articles")
    job = store.claim_many("dedup", [article["hash"]], "w1")[0]
    assert store.finish(job, dict(next="fetch"))
    job = store.claim_many("fetch", [article["hash"]], "w1")[0]
    output = engine.execute(job)
    assert output["next"] == "context"
    assert store.finish(job, output)
    [after] = rows(store, "SELECT * FROM bm_articles")
    assert (after["stage"], after["state"], after["attempts"]) == ("context", "pending", 0)
    assert after["payload"]["title"] and after["payload"]["body"]
    assert after["history"][-1]["stage"] == "fetch"


def test_a_transient_http_failure_goes_through_the_retry_path(world):
    store, engine, data = world["store"], world["engine"], world["data"]
    run_discovery(world)
    [article] = rows(store, "SELECT hash FROM bm_articles")
    job = store.claim_many("dedup", [article["hash"]], "w1")[0]
    store.finish(job, dict(next="fetch"))
    data[STORY] = FetchResult(
        STORY, 503, b"", "HTTP 503", final_url=STORY, content_type="text/html"
    )
    job = store.claim_many("fetch", [article["hash"]], "w1")[0]
    with pytest.raises(RetryableError) as error:
        engine.execute(job)
    state = store.failure(job, str(error.value), metrics=error.value.metrics)
    assert state == "retry"
    [after] = rows(store, "SELECT * FROM bm_articles")
    assert after["state"] == "retry" and after["stage"] == "fetch"


def test_maintain_keeps_a_run_with_open_articles_running(world):
    store = world["store"]
    run = run_discovery(world)
    store.maintain()
    [state] = rows(store, "SELECT status FROM bm_runs WHERE id=%s", run["id"])
    assert state["status"] == "running"
    rows(store, "SELECT 1")  # a conexão do store é por operação: nada fica preso
