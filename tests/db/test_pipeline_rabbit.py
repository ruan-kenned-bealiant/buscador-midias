"""O fluxo completo pelas filas RabbitMQ reais: publisher e workers em threads."""

from __future__ import annotations

import os
import threading
import time
import uuid
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
from buscador_midias.pipeline import broker
from buscador_midias.pipeline.service import Pipeline

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1" or os.environ.get("BM_TEST_RABBIT") != "1",
    reason="Requer PostgreSQL e RabbitMQ de teste explícitos.",
)

ROOT = "https://news.example/"
FEED = ROOT + "feed"
STORY = ROOT + "noticia/hoje-na-cidade"


@pytest.fixture
def prefix(monkeypatch: pytest.MonkeyPatch):
    name = "bm.test." + uuid.uuid4().hex[:12]
    monkeypatch.setenv("BM_QUEUE_PREFIX", name)
    yield name
    connection = broker.connect()
    try:
        broker.delete_all(connection.channel())
    finally:
        connection.close()


def build(schema_name: str, tmp_path: Path) -> tuple[WorkStore, Pipeline]:
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
    store.start_pool(max_size=12)
    return store, Pipeline(store, engine, lease_interval=5)


def test_a_story_crosses_all_six_queues_and_reaches_the_corpus(schema_name, tmp_path, prefix):
    store, pipeline = build(schema_name, tmp_path)
    threads = []
    for stage in broker.STAGES:
        threads.append(threading.Thread(target=pipeline.publisher, args=(stage, "primary")))
        threads.append(threading.Thread(target=pipeline.worker, args=(stage, 0, "primary")))
    try:
        store.create_run({})
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            done = rows(store, "SELECT outcome FROM bm_articles WHERE state='done'")
            if done:
                break
            time.sleep(0.5)
        assert done and done[0]["outcome"] == "persisted"
        store.maintain()
        [run] = rows(store, "SELECT status FROM bm_runs")
        assert run["status"] == "completed"
        # os consumidores aparecem no broker: a base do painel de gargalos
        connection = broker.connect()
        try:
            consumers = [broker.depth(connection.channel(), s)[1] for s in broker.STAGES]
        finally:
            connection.close()
        assert all(c >= 1 for c in consumers), consumers
    finally:
        pipeline.stop.set()
        for thread in threads:
            thread.join(timeout=30)
        store.close_pool()
    assert len(Writer.saved) == 1


def test_a_message_for_an_article_that_is_not_waiting_is_acknowledged_and_ignored(
    schema_name, tmp_path, prefix
):
    store, pipeline = build(schema_name, tmp_path)
    connection = broker.connect()
    try:
        channel = connection.channel()
        broker.declare(channel)
        broker.publish(channel, "fetch", "f" * 64)
        thread = threading.Thread(target=pipeline.worker, args=("fetch", 0, "primary"))
        thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and broker.depth(channel, "fetch")[0] > 0:
            time.sleep(0.2)
        assert broker.depth(channel, "fetch")[0] == 0
    finally:
        pipeline.stop.set()
        thread.join(timeout=30)
        connection.close()
        store.close_pool()
