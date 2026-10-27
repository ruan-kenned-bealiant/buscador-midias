"""Filas de ingestão em PostgreSQL isolado: sem chamadas pagas ou corpus real."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_catalog_testing import FakeReader, article

from buscador_midias.analysis.context import ContextVerdict
from buscador_midias.analysis.entity_context import EntityContext
from buscador_midias.catalog.migrate_postgres import migrate_catalog
from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.catalog.store import CatalogError
from buscador_midias.corpus.writer import WriteOutcome
from buscador_midias.evidence.storage import LocalEvidenceStore
from buscador_midias.ingestion.artifacts import Artifacts
from buscador_midias.ingestion.engine import Engine
from buscador_midias.ingestion.runtime import Service
from buscador_midias.ingestion.store import IngestStore
from buscador_midias.net.fetcher import FetchResult

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.fixture
def ingest(local, schema):  # noqa: F811
    migrate_catalog(local.path, schema)
    repo = IngestStore(PostgresCatalogStore(schema))
    repo.initialize()
    config = repo.settings()
    repo.configure(
        dict(
            version=config["version"],
            config=dict(window_mode="lookback", schedule_mode="link_interval"),
        )
    )
    return repo


def next_job(repo, stage):
    with repo.connection() as db:
        row = db.execute(
            "SELECT id FROM ingest_jobs WHERE stage=%s AND state IN "
            "('pending','queued','retry') ORDER BY created_at LIMIT 1",
            (stage,),
        ).fetchone()
    return repo.claim(row["id"], "test-worker")


def test_corpus_articles_are_scoped_to_source_run_and_real_records(ingest):
    from buscador_midias.ingestion.progress import corpus_articles

    source = ingest.store.save("source", dict(name="Outra", url="https://outra.example"))
    ingest.store.save(
        "endpoint", dict(url="https://outra.example/feed", kind="feed"), source_id=source["id"]
    )
    ingest.store.save("endpoint", dict(url="https://jose.example/second", kind="feed"), source_id=1)
    run = ingest.create_run({})
    shared = "https://shared.example/noticia"
    for _ in range(3):
        job = next_job(ingest, "discovery")
        extra = (
            "https://outra.example/only"
            if job["snapshot"]["source"]["id"] == source["id"]
            else "https://jose.example/not-registered"
        )
        ingest.finish(job, dict(candidates=[dict(url=shared), dict(url=extra)]))
    with ingest.connection() as db:
        db.execute(
            "CREATE TABLE midias_ia_articles(id bigint,titulo text,url text,"
            "published_at timestamptz,evidencia_status text,evidencia_path text)"
        )
        db.execute(
            "INSERT INTO midias_ia_articles VALUES(1,'Compartilhada',%s,now(),'captured',"
            "'midias-ia/evidencias/test.html'),"
            "(2,'Outra notícia','https://outra.example/only',now(),'captured','midias-ia/evidencias/other.html')",
            (shared,),
        )
        db.execute(
            "UPDATE ingest_articles SET article_id=CASE WHEN url=%s THEN 1 "
            "WHEN url='https://outra.example/only' THEN 2 ELSE 999 END",
            (shared,),
        )
    result = corpus_articles(ingest, 1, run["id"], corpus_schema=ingest.store.schema)
    assert result["total"] == 1
    assert result["articles"][0]["title"] == "Compartilhada"
    assert result["articles"][0]["evidence_url"].startswith("/evidencias/corpus/")
    other = corpus_articles(ingest, source["id"], run["id"], corpus_schema=ingest.store.schema)
    assert other["total"] == 2
    assert (
        len(
            corpus_articles(ingest, source["id"], run["id"], 1, corpus_schema=ingest.store.schema)[
                "articles"
            ]
        )
        == 1
    )
    with pytest.raises(CatalogError):
        corpus_articles(ingest, 1, "f" * 32, corpus_schema=ingest.store.schema)


def test_manual_round_rejects_zero_length_window(ingest):
    from datetime import UTC, datetime

    instant = datetime.now(UTC).isoformat()
    with pytest.raises(CatalogError):
        ingest.create_run(dict(since=instant, until=instant))
    with ingest.connection() as db:
        assert db.execute("SELECT count(*) n FROM ingest_runs").fetchone()["n"] == 0


def test_source_progress_follows_pipeline_and_deduplicates_links(ingest):
    from buscador_midias.ingestion.progress import source_progress

    ingest.store.save("endpoint", dict(url="https://jose.example/second", kind="feed"), source_id=1)
    run = ingest.create_run({})
    assert source_progress(ingest, run["id"])["sources"][0]["progress"] is None
    for _ in range(2):
        ingest.finish(
            next_job(ingest, "discovery"),
            dict(candidates=[dict(url="https://jose.example/noticia")]),
        )
    ingest.maintain()
    item = source_progress(ingest, run["id"])["sources"][0]
    assert item["links"] == 2 and item["articles"] == 1
    before = item["progress"]
    ingest.finish(next_job(ingest, "dedup"), dict(next="fetch"))
    item = source_progress(ingest, run["id"])["sources"][0]
    assert before < item["progress"] < 100
    ingest.finish(next_job(ingest, "fetch"), dict(status="outside_window"))
    ingest.maintain()
    item = source_progress(ingest, run["id"])["sources"][0]
    assert item["progress"] == 100 and item["state"] == "completed"
    assert item["statuses"] == dict(outside_window=1)


def test_shared_work_is_visible_for_both_source_origins(ingest):
    from buscador_midias.ingestion.progress import source_progress

    source = ingest.store.save("source", dict(name="Outra fonte", url="https://outra.example"))
    ingest.store.save(
        "endpoint", dict(url="https://outra.example/feed", kind="feed"), source_id=source["id"]
    )
    run = ingest.create_run({})
    for _ in range(2):
        ingest.finish(
            next_job(ingest, "discovery"),
            dict(candidates=[dict(url="https://shared.example/noticia")]),
        )
    ingest.maintain()
    dedup = next_job(ingest, "dedup")
    ingest.failure(dedup, "Falha simulada")
    report = source_progress(ingest, run["id"])
    assert len(report["sources"]) == 2
    for item in report["sources"]:
        assert item["articles"] == 1
        assert any(r["stage"] == "dedup" and r["state"] == "retry" for r in item["stages"])
        tasks = ingest.overview(source_id=item["id"], run_id=run["id"])
        assert dedup["id"] in {j["id"] for j in tasks["jobs"]}
    assert source_progress(ingest, run["id"], search="Outra")["total"] == 1


def test_source_identifies_inherited_failure_without_inventing_current_failed_task(ingest):
    from buscador_midias.ingestion.progress import source_progress

    settings = ingest.settings()
    ingest.configure(dict(version=settings["version"], config=dict(max_attempts=1)))
    previous = ingest.create_run({})
    url = "https://jose.example/noticia"
    ingest.finish(next_job(ingest, "discovery"), dict(candidates=[dict(url=url)]))
    ingest.finish(next_job(ingest, "dedup"), dict(next="fetch"))
    failed = next_job(ingest, "fetch")
    ingest.failure(failed, "bright_data_api_error: proxy HTTP 407")
    ingest.maintain()
    old = source_progress(ingest, previous["id"])["sources"][0]
    assert old["current_errors"] == 1 and old["historical_errors"] == 0
    assert old["statuses"] == {"failed": 1}
    assert old["inherited_failures"] == []

    current = ingest.create_run({})
    ingest.finish(next_job(ingest, "discovery"), dict(candidates=[dict(url=url), dict(url=url)]))
    ingest.maintain()
    item = source_progress(ingest, current["id"])["sources"][0]
    assert item["progress"] == 100 and item["state"] == "partial"
    assert item["articles"] == 1
    assert item["statuses"] == {"previous_failed": 1}
    assert item["current_errors"] == 0 and item["historical_errors"] == 1
    assert item["stages"] == [dict(stage="discovery", state="succeeded", n=1)]
    issue = item["inherited_failures"][0]
    assert issue["run_id"] == previous["id"]
    assert issue["job_id"] == failed["id"] and issue["stage"] == "fetch"
    assert issue["url"] == url and "407" in issue["outcome"]
    # A consulta não dispara reprocessamento nem reescreve o resultado anterior.
    with ingest.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) n FROM ingest_jobs WHERE run_id=%s", (current["id"],)
            ).fetchone()["n"]
            == 1
        )
        assert (
            db.execute("SELECT state FROM ingest_jobs WHERE id=%s", (failed["id"],)).fetchone()[
                "state"
            ]
            == "failed"
        )


def test_daily_window_and_archive_link_exclusion(ingest):
    from datetime import datetime

    from buscador_midias.ingestion.flow import today_start

    config = ingest.settings()
    ingest.configure(dict(version=config["version"], config=dict(window_mode="today")))
    run = ingest.create_run({})
    assert run["since"] == today_start(run["until"])
    with pytest.raises(CatalogError):
        ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    job = next_job(ingest, "discovery")
    ingest.finish(job, dict(outcome="sem_novidades"))
    ingest.maintain()
    with ingest.connection() as db:
        db.execute(
            "INSERT INTO ingest_daily_routes VALUES(1,'archive','arquivo',%s)",
            (datetime.now().astimezone(),),
        )
    with pytest.raises(CatalogError):
        ingest.create_run({})


def test_archival_move_preserves_backlog_and_uses_fresh_tables(ingest, tmp_path):
    from psycopg import sql

    from buscador_midias.ingestion.daily_migration import archive_backlog

    run = ingest.create_run({})
    setting = ingest.settings()
    ingest.configure(dict(version=setting["version"], config=dict(workers_enabled=False)))
    manifest = archive_backlog(schema=ingest.store.schema, output=tmp_path)
    try:
        with ingest.connection() as db:
            assert db.execute("SELECT count(*) n FROM ingest_runs").fetchone()["n"] == 0
            old = db.execute(
                sql.SQL("SELECT id FROM {}.ingest_runs").format(
                    sql.Identifier(manifest["schema_name"])
                )
            ).fetchone()
            assert old["id"] == run["id"]
            assert db.execute("SELECT count(*) n FROM links").fetchone()["n"] == 1
        assert ingest.settings()["config"]["window_mode"] == "today"
        assert ingest.archive_history()["archives"][0]["runs"][0]["id"] == run["id"]
    finally:
        with ingest.connection() as db:
            db.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(manifest["schema_name"]))
            )


def test_daily_index_skips_archives_and_flags_children_without_time(ingest):
    from datetime import timedelta

    from buscador_midias.catalog.mechanisms import get_mechanism

    setting = ingest.settings()
    ingest.configure(dict(version=setting["version"], config=dict(window_mode="today")))
    ingest.create_run({})
    job = next_job(ingest, "discovery")
    job["snapshot"].update(
        model="sitemap_tree", kind="sitemap", parameters=get_mechanism("sitemap_tree").defaults()
    )
    recent = job["run"]["since"].isoformat()
    old = (job["run"]["since"] - timedelta(days=10)).isoformat()
    body = (
        f"<sitemapindex><sitemap><loc>https://jose.example/recent.xml</loc>"
        f"<lastmod>{recent}</lastmod></sitemap>"
        f"<sitemap><loc>https://jose.example/old.xml</loc><lastmod>{old}</lastmod></sitemap>"
        "<sitemap><loc>https://jose.example/unknown.xml</loc></sitemap>"
        "</sitemapindex>"
    ).encode()
    engine = Engine(
        ingest,
        reader_factory=lambda roots, progress: FakeReader({job["payload"]["url"]: body}, progress),
    )
    result = engine.discovery(job)
    assert result["children"] == ["https://jose.example/recent.xml"]
    assert result["coverage_partial"]
    assert result["unrecognized_children"] == ["https://jose.example/unknown.xml"]
    assert result["daily_routes_skipped"] == 1


def test_network_publisher_keeps_one_job_per_host_in_transit(ingest, monkeypatch):
    from buscador_midias.ingestion import broker

    ingest.create_run({})
    job = next_job(ingest, "discovery")
    ingest.finish(
        job,
        dict(
            candidates=[
                dict(url=u)
                for u in (
                    "https://jose.example/a",
                    "https://jose.example/b",
                    "https://other.example/c",
                )
            ]
        ),
    )
    for _ in range(3):
        ingest.finish(next_job(ingest, "dedup"), dict(next="fetch"))
    sent = []
    monkeypatch.setattr(broker, "publish", lambda ch, stage, ident, **kw: sent.append(ident))
    service = Service(ingest)
    assert service.publish_once(None, "fetch") == 2
    assert service.publish_once(None, "fetch") == 0
    assert len(sent) == 2


def test_fixed_schedule_is_durable_and_ignores_link_cadence(ingest, monkeypatch):
    from datetime import UTC, datetime

    from buscador_midias.ingestion import store as store_module

    setting = ingest.settings()
    ingest.configure(
        dict(
            version=setting["version"],
            config=dict(
                enabled=True, schedule_mode="clock", schedule_hours=[6, 12, 16], window_mode="today"
            ),
        )
    )

    class Clock(datetime):
        instant = datetime(2026, 10, 24, 9, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(store_module, "datetime", Clock)
    morning = ingest.create_run({}, scheduled=True)
    assert morning is not None
    assert ingest.create_run({}, scheduled=True) is None
    ingest.finish(next_job(ingest, "discovery"), dict(outcome="sem_novidades"))
    ingest.maintain()
    with ingest.connection() as db:
        db.execute("UPDATE ingest_schedule SET next_due='2099-01-01'")
    Clock.instant = datetime(2026, 10, 24, 13, tzinfo=UTC)
    assert ingest.create_run({}, scheduled=True) is None
    Clock.instant = datetime(2026, 10, 24, 15, tzinfo=UTC)
    noon = ingest.create_run({}, scheduled=True)
    assert noon and noon["id"] != morning["id"]
    assert ingest.create_run({}, scheduled=True) is None
    with ingest.connection() as db:
        assert db.execute("SELECT count(*) n FROM ingest_clock_runs").fetchone()["n"] == 2


def test_full_document_cache_is_separated_by_revisions(ingest):
    from copy import deepcopy

    from buscador_midias.catalog.mechanisms.parsing import parse_document

    ingest.create_run({})
    job = next_job(ingest, "discovery")
    url = job["payload"]["url"]
    body = (
        b"<sitemapindex><sitemap><loc>https://jose.example/old.xml</loc></sitemap></sitemapindex>"
    )
    parsed = parse_document(body, url)
    assert ingest.cached_document(job) is None
    ingest.cache_document(job, parsed, FetchResult(url, 200, body, None, etag='"v1"'))
    cached = ingest.cached_document(job)
    assert cached["document"] == parsed
    assert cached["etag"] == '"v1"'
    revised = deepcopy(job)
    revised["snapshot"]["link_version"] += 1
    assert ingest.cached_document(revised) is None


def test_g1_conditional_response_reuses_full_index_and_reselects_window(ingest):
    from datetime import UTC, datetime

    from buscador_midias.catalog.mechanisms import get_mechanism

    root = "https://g1.globo.com/sitemap/g1/sitemap.xml"
    child = "https://g1.globo.com/sitemap/g1/2026/10/20_1.xml"
    old = "https://g1.globo.com/sitemap/g1/2001/01/01_1.xml"
    body = (
        f"<sitemapindex><sitemap><loc>{child}</loc></sitemap>"
        f"<sitemap><loc>{old}</loc></sitemap></sitemapindex>"
    ).encode()
    ingest.create_run({})
    job = next_job(ingest, "discovery")
    job["snapshot"].update(
        model="g1_daily", kind="sitemap", url=root, parameters=get_mechanism("g1_daily").defaults()
    )
    job["snapshot"]["source"]["url"] = "https://g1.globo.com"
    job["payload"]["url"] = root
    job["run"].update(
        since=datetime(2026, 10, 20, tzinfo=UTC), until=datetime(2026, 10, 20, 23, tzinfo=UTC)
    )

    class ConditionalReader(FakeReader):
        conditional_headers = {}

    responses = [FetchResult(root, 200, body, None, etag='"v1"'), FetchResult(root, 304, b"", None)]
    readers = []

    def factory(roots, progress):
        instance = ConditionalReader({root: responses.pop(0)}, progress)
        instance.conditional_headers = {}
        readers.append(instance)
        return instance

    engine = Engine(ingest, reader_factory=factory)
    first = engine.discovery(job)
    assert first["children"] == [child]
    job["run"].update(
        since=datetime(2001, 1, 1, tzinfo=UTC), until=datetime(2001, 1, 1, 23, tzinfo=UTC)
    )
    second = engine.discovery(job)
    assert second["children"] == [old]
    assert second["metrics"]["not_modified_responses"] == 1
    assert readers[1].conditional_headers[root] == {"If-None-Match": '"v1"'}


def test_schedule_manual_window_and_no_overlapping_read(ingest):
    assert ingest.create_run({}, scheduled=True) is None
    config = ingest.settings()
    ingest.configure(dict(version=config["version"], config=dict(enabled=True)))
    run = ingest.create_run({}, scheduled=True)
    assert run["links"] == 1
    assert ingest.create_run({}, scheduled=True) is None
    with pytest.raises(CatalogError):
        ingest.create_run(dict(link_ids=[True]))
    with pytest.raises(CatalogError):
        ingest.create_run(dict(since="2020-01-01"))
    job = next_job(ingest, "discovery")
    ingest.finish(job, dict(outcome="sem_novidades"))
    ingest.maintain()
    with ingest.connection() as db:
        state = db.execute("SELECT state FROM ingest_runs WHERE id=%s", (run["id"],)).fetchone()[
            "state"
        ]
    assert state == "completed"
    assert ingest.create_run({}, scheduled=True) is None
    assert ingest.create_run(dict(link_ids=[1]))["links"] == 1


def test_shared_article_has_one_pipeline_and_keeps_both_origins(ingest):
    other = ingest.store.save(
        "endpoint", dict(url="https://jose.example/another-feed", kind="feed"), source_id=1
    )
    run = ingest.create_run({})
    assert run["links"] == 2
    for _ in range(2):
        job = next_job(ingest, "discovery")
        ingest.finish(job, dict(candidates=[dict(url="https://jose.example/noticia")]))
    with ingest.connection() as db:
        assert db.execute("SELECT count(*) n FROM ingest_occurrences").fetchone()["n"] == 2
        assert (
            db.execute("SELECT count(*) n FROM ingest_jobs WHERE stage='dedup'").fetchone()["n"]
            == 1
        )
        assert db.execute("SELECT count(*) n FROM ingest_outbox").fetchone()["n"] == 3
    assert other["id"] > 1


def test_message_replay_and_owner_guard(ingest):
    ingest.create_run({})
    job = next_job(ingest, "discovery")
    assert ingest.claim(job["id"], "another") is None
    assert not ingest.finish(dict(job, owner="another"), dict(outcome="ok"))
    assert ingest.finish(job, dict(outcome="ok"))
    assert ingest.claim(job["id"], "another") is None


def test_retry_budget_counts_failures_not_host_contention(ingest):
    ingest.create_run({})
    for _index in range(5):
        job = next_job(ingest, "discovery")
        assert ingest.failure(job, "host ocupado", busy=True) == "retry"
        with ingest.connection() as db:
            db.execute("UPDATE ingest_jobs SET available_at=now() WHERE id=%s", (job["id"],))
    for index in range(3):
        job = next_job(ingest, "discovery")
        state = ingest.failure(job, "HTTP 403")
        assert state == ("failed" if index == 2 else "retry")
        with ingest.connection() as db:
            db.execute("UPDATE ingest_jobs SET available_at=now() WHERE id=%s", (job["id"],))
    with ingest.connection() as db:
        assert (
            db.execute("SELECT count(*) n FROM ingest_outbox WHERE kind='failed'").fetchone()["n"]
            == 1
        )
    ingest.maintain()
    assert ingest.overview()["runs"][0]["state"] == "partial"
    ingest.retry(job["id"])
    assert ingest.claim(job["id"], "test-worker")["failures"] == 0


def test_outbox_publish_failure_rolls_back_and_can_replay(ingest, monkeypatch):
    ingest.create_run({})
    calls = []

    def publish(channel, stage, ident, **kw):
        calls.append(ident)
        raise OSError("broker offline")

    monkeypatch.setattr("buscador_midias.ingestion.broker.publish", publish)
    with pytest.raises(OSError):
        Service(ingest).publish_once(None)
    with ingest.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) n FROM ingest_outbox WHERE published_at IS NULL"
            ).fetchone()["n"]
            == 1
        )
    monkeypatch.setattr("buscador_midias.ingestion.broker.publish", lambda *args, **kw: None)
    assert Service(ingest).publish_once(None) == 1
    assert Service(ingest).publish_once(None) == 0


class Analyzer:
    def analyze_entities(self, item, entities):
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

    def analyze(self, items):
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


def reader(roots, progress):
    base = roots[0].rstrip("/")
    return FakeReader(
        {
            roots[
                1
            ]: f"<rss><channel><item><link>{base}/noticia</link></item></channel></rss>".encode(),
            base + "/noticia": FetchResult(
                base + "/noticia",
                200,
                article().replace(
                    b"A reportagem ouviu", "João Silva participou. A reportagem ouviu".encode()
                ),
                None,
                base + "/noticia",
                "text/html",
            ),
        },
        progress,
    )


def test_pipeline_separate_stages_through_text_evidence_and_persistence(
    ingest, tmp_path, monkeypatch
):
    setting = ingest.settings()
    ingest.configure(dict(version=setting["version"], config=dict(allow_undated=True)))
    saved = []

    class ClassifiedAnalyzer(Analyzer):
        def analyze(self, items):
            from dataclasses import replace

            return [
                replace(
                    super().analyze(items)[0],
                    content_category="opinion",
                    article_country="PT",
                    article_country_evidence="Edição portuguesa",
                )
            ]

    class Writer:
        def source_countries(self):
            return {"jose.example": "GB"}

        def write(self, articles):
            assert articles[0].body and "João Silva" in articles[0].body
            saved.extend(articles)
            return [WriteOutcome(articles[0].url_hash, 123, True, False, 1, 1)]

    engine = Engine(
        ingest,
        artifacts=Artifacts(tmp_path / "work"),
        reader_factory=reader,
        analyzer_factory=ClassifiedAnalyzer,
        evidence_store=LocalEvidenceStore(tmp_path / "evidence"),
        writer_factory=Writer,
    )
    monkeypatch.setattr(engine, "dedup", lambda job: dict(next="fetch", outcome="nova_url"))
    ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    for stage in ["discovery", "dedup", "fetch", "context", "evidence", "persist"]:
        job = next_job(ingest, stage)
        output = engine.execute(job)
        ingest.finish(job, output)
        with ingest.connection() as db:
            assert (
                db.execute("SELECT state FROM ingest_jobs WHERE id=%s", (job["id"],)).fetchone()[
                    "state"
                ]
                == "succeeded"
            )
    ingest.maintain()
    assert ingest.overview()["runs"][0]["state"] == "completed"
    assert len(saved) == 1 and saved[0].entities[0].name == "João Silva"
    assert len(saved[0].entity_contexts) == 1
    assert saved[0].entity_contexts[0].name == "João Silva"
    assert saved[0].entity_contexts[0].alert is False
    assert saved[0].evidence.status == "captured"
    assert saved[0].content_category == "opinion"
    assert (saved[0].source_country, saved[0].source_country_method) == ("PT", "article")
    with ingest.connection() as db:
        result = db.execute("SELECT * FROM ingest_articles").fetchone()
        assert result["status"] == "persisted" and result["article_id"] == 123
        assert not (tmp_path / "work" / (result["hash"] + ".html.gz")).exists()


def test_configuration_validation_and_revision(ingest):
    config = ingest.settings()
    with pytest.raises(CatalogError):
        ingest.configure(dict(version=config["version"], config=dict(lookback_minutes=True)))
    with pytest.raises(CatalogError):
        ingest.configure(dict(version=config["version"], config=dict(workers={"context": 1})))
    ingest.configure(dict(version=config["version"], config=dict(lookback_minutes=60)))
    with pytest.raises(CatalogError):
        ingest.configure(dict(version=config["version"], config=dict(enabled=True)))


def test_expired_worker_lease_retries_and_stops_after_budget(ingest):
    ingest.create_run({})
    for _attempt in range(3):
        job = next_job(ingest, "discovery")
        with ingest.connection() as db:
            db.execute(
                "UPDATE ingest_jobs SET lease_until=now()-interval '1 second' WHERE id=%s",
                (job["id"],),
            )
        ingest.maintain()
    with ingest.connection() as db:
        row = db.execute(
            "SELECT state,failures FROM ingest_jobs WHERE id=%s", (job["id"],)
        ).fetchone()
    assert row == dict(state="failed", failures=3)
    assert not ingest.finish(job, dict(outcome="resposta tardia"))


def test_full_pipeline_using_real_rabbit_queues(ingest, tmp_path, monkeypatch):
    if os.environ.get("BM_TEST_RABBIT") != "1":
        pytest.skip("Requer RabbitMQ de teste explícito")
    import uuid

    from buscador_midias.ingestion import broker

    prefix = "bm.test." + uuid.uuid4().hex
    monkeypatch.setenv("BM_QUEUE_PREFIX", prefix)

    class Writer:
        def write(self, articles):
            assert articles[0].body and "João Silva" in articles[0].body
            return [WriteOutcome(articles[0].url_hash, 321, True, False, 1, 1)]

    engine = Engine(
        ingest,
        artifacts=Artifacts(tmp_path / "work"),
        reader_factory=reader,
        analyzer_factory=Analyzer,
        evidence_store=LocalEvidenceStore(tmp_path / "evidence"),
        writer_factory=Writer,
    )
    monkeypatch.setattr(engine, "dedup", lambda job: dict(next="fetch", outcome="nova_url"))
    service = Service(ingest, engine)
    ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    connection = broker.connect()
    channel = connection.channel()
    broker.declare(channel)
    try:
        for stage in ["discovery", "dedup", "fetch", "context", "evidence", "persist"]:
            assert service.publish_once(channel) == 1
            method, props, body = channel.basic_get(queue=broker.queue_name(stage), auto_ack=False)
            assert method and props.delivery_mode == 2
            import json

            message = json.loads(body)
            assert set(message) == {"job_id"}
            if stage == "fetch":
                job = ingest.claim(message["job_id"], "rabbit-first", recovery=False)
                ingest.failure(job, "HTTP 503 simulado")
                channel.basic_ack(method.delivery_tag)
                with ingest.connection() as db:
                    db.execute(
                        "UPDATE ingest_jobs SET available_at=now() WHERE id=%s", (job["id"],)
                    )
                    db.execute(
                        "UPDATE ingest_outbox SET ready_at=now() WHERE job_id=%s", (job["id"],)
                    )
                assert service.publish_once(channel, stage) == 0
                assert service.publish_once(channel, stage, recovery=True) == 1
                method, props, body = channel.basic_get(
                    queue=broker.queue_name(stage, recovery=True), auto_ack=False
                )
                assert method and props.delivery_mode == 2
                assert service.process(json.loads(body)["job_id"], "rabbit-recovery", recovery=True)
            else:
                assert service.process(message["job_id"], "rabbit-test", channel)
            channel.basic_ack(method.delivery_tag)
        ingest.maintain()
        assert ingest.overview()["runs"][0]["state"] == "completed"
    finally:
        for stage in ["discovery", "dedup", "fetch", "context", "evidence", "persist"]:
            channel.queue_delete(queue=broker.queue_name(stage))
            channel.queue_delete(queue=broker.queue_name(stage) + ".failed")
            channel.queue_delete(queue=broker.queue_name(stage, recovery=True))
        connection.close()


def test_wider_window_reconsiders_old_article_without_erasing_attempt_history(ingest):
    from datetime import UTC, datetime, timedelta

    ingest.create_run({})
    discovery = next_job(ingest, "discovery")
    candidate = dict(url="https://jose.example/noticia")
    ingest.finish(discovery, dict(candidates=[candidate, candidate]))
    dedup = next_job(ingest, "dedup")
    old_date = datetime.now(UTC) - timedelta(days=10)
    ingest.finish(
        dedup, dict(status="outside_window", article=dict(published_at=old_date.isoformat()))
    )
    ingest.maintain()
    second = ingest.create_run(dict(since=(old_date - timedelta(days=1)).isoformat()))
    discovery = next_job(ingest, "discovery")
    ingest.finish(discovery, dict(candidates=[candidate, candidate]))
    new = next_job(ingest, "dedup")
    assert new["generation"] == 2
    assert new["run_id"] == second["id"]
    with ingest.connection() as db:
        assert db.execute("SELECT count(*) n FROM ingest_articles").fetchone()["n"] == 1
        assert (
            db.execute(
                "SELECT count(*) n FROM ingest_attempts WHERE job_id=%s", (dedup["id"],)
            ).fetchone()["n"]
            == 1
        )


def test_provider_failure_defers_stage_without_rejecting_source(ingest):
    from buscador_midias.ingestion.engine import ProviderUnavailableError

    class Unavailable:
        def execute(self, job):
            raise ProviderUnavailableError("OpenRouter indisponível: HTTP 429", {"errors": 1})

    ingest.create_run({})
    with ingest.connection() as db:
        ident = db.execute("SELECT id FROM ingest_jobs").fetchone()["id"]
    service = Service(ingest, Unavailable())
    assert service.process(ident, "test-provider")
    with ingest.connection() as db:
        job = db.execute("SELECT * FROM ingest_jobs WHERE id=%s", (ident,)).fetchone()
    assert job["state"] == "retry"
    assert job["failures"] == 0
    assert job["available_at"] > job["updated_at"]
    assert "discovery" in service.cooldowns


def test_retry_uses_current_config_and_preserves_original_run_config(ingest):
    ingest.create_run({})
    for _ in range(3):
        job = next_job(ingest, "discovery")
        ingest.failure(job, "HTTP 500")
        with ingest.connection() as db:
            db.execute("UPDATE ingest_jobs SET available_at=now() WHERE id=%s", (job["id"],))
    old_model = job["run"]["config"]["model"]
    setting = ingest.settings()
    ingest.configure(dict(version=setting["version"], config=dict(model="example/model-v2")))
    ingest.retry(job["id"])
    retried = ingest.claim(job["id"], "new-worker")
    assert retried["run"]["config"]["model"] == "example/model-v2"
    with ingest.connection() as db:
        run = db.execute("SELECT config FROM ingest_runs WHERE id=%s", (job["run_id"],)).fetchone()
    assert run["config"]["model"] == old_model


def test_costs_are_json_serializable_without_precision_loss():
    import json
    from decimal import Decimal

    value = IngestStore.jsonable(dict(metrics=[dict(cost_usd=Decimal("0.0084612"))]))
    assert json.loads(json.dumps(value))["metrics"][0]["cost_usd"] == "0.0084612"


def test_stage_publisher_does_not_wait_behind_older_discovery(ingest, monkeypatch):
    run = ingest.create_run({})
    with ingest.connection() as db:
        poll = db.execute("SELECT id FROM ingest_polls").fetchone()["id"]
        for i in range(120):
            ingest.enqueue(db, "dedup", str(i), run["id"], poll, {})
        ingest.enqueue(db, "persist", "ready-to-save", run["id"], poll, {})
    sent = []
    monkeypatch.setattr(
        "buscador_midias.ingestion.broker.publish",
        lambda ch, stage, ident, **kw: sent.append(stage),
    )
    assert Service(ingest).publish_once(None, "persist") == 1
    assert sent == ["persist"]
    with ingest.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) n FROM ingest_outbox WHERE published_at IS NULL"
            ).fetchone()["n"]
            == 121
        )


def test_batch_claim_and_commit_preserve_individual_checkpoints(ingest):
    ingest.create_run({})
    discovery = next_job(ingest, "discovery")
    ingest.finish(
        discovery, dict(candidates=[dict(url=f"https://jose.example/news/{i}") for i in range(100)])
    )
    with ingest.connection() as db:
        ids = [
            r["id"] for r in db.execute("SELECT id FROM ingest_jobs WHERE stage='dedup'").fetchall()
        ]
    jobs = ingest.claim_many(ids, "batch-owner")
    assert len(jobs) == 100
    assert ingest.claim_many(ids, "another-owner") == []
    with pytest.raises(ValueError):
        ingest.finish_many(jobs, [dict(status="known", outcome="existente")])
    with ingest.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) n FROM ingest_jobs WHERE stage='dedup' AND state='running'"
            ).fetchone()["n"]
            == 100
        )
    ingest.finish_many(jobs, [dict(status="known", outcome="existente") for _ in jobs])
    with ingest.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) n FROM ingest_attempts WHERE outcome='succeeded'"
            ).fetchone()["n"]
            == 101
        )


@pytest.mark.parametrize("evidence_status", ["captured", "pending"])
def test_corpus_lookup_shares_connection_and_preserves_repair(ingest, evidence_status):
    from buscador_midias.domain.url import url_hash

    ingest.create_run({})
    discovery = next_job(ingest, "discovery")
    urls = [f"https://jose.example/news/{i}" for i in range(20)]
    ingest.finish(discovery, dict(candidates=[dict(url=u) for u in urls]))
    with ingest.connection() as db:
        db.execute(
            "CREATE TABLE midias_ia_articles(id bigint,url_hash text,evidencia_status text,"
            "evidencia_path text,body text,ai_model text,ai_verdict text,ai_reason text,"
            "ai_summary text,resumo text,ai_score numeric,content_category text,"
            "source_country text)"
        )
        db.execute(
            "CREATE TABLE midias_ia_article_entities(article_id bigint,name text,"
            "entity_type text,role text)"
        )
        db.execute(
            "INSERT INTO midias_ia_articles VALUES(1,%s,%s,'evidence.html',"
            "'Texto completo','model','neutro','','','',0.9,NULL,NULL)",
            (url_hash(urls[0]), evidence_status),
        )
        db.execute("INSERT INTO midias_ia_article_entities VALUES(1,'João Silva','PER','citado')")
        ids = [
            r["id"] for r in db.execute("SELECT id FROM ingest_jobs WHERE stage='dedup'").fetchall()
        ]
    jobs = ingest.claim_many(ids, "batch")

    class Spy:
        calls = 0

        def connection(self):
            self.calls += 1
            return ingest.connection()

    spy = Spy()
    engine = Engine(spy, corpus_schema=ingest.store.schema)
    results = engine.dedup_many(jobs)
    assert spy.calls == 1
    assert sum(r.get("status") == "known" for r in results) == int(evidence_status == "captured")
    assert sum(r.get("next") == "fetch" for r in results) == (
        19 if evidence_status == "captured" else 20
    )
    if evidence_status == "pending":
        repaired = next(r for r in results if r.get("article_id") == 1)
        assert repaired["outcome"] == "existente_reparar_evidencia"
        assert repaired["article"]["analysis"]["entities"][0]["name"] == "João Silva"


def test_publication_filter_does_not_trust_unknown_legacy_dates(ingest):
    from datetime import UTC, datetime, timedelta

    from buscador_midias.ingestion.flow import publication_outside

    ingest.create_run({})
    job = next_job(ingest, "discovery")
    job["article"] = dict(
        payload=dict(published_hint=(datetime.now(UTC) - timedelta(days=100)).isoformat())
    )
    assert publication_outside(job) is None
    job["article"]["payload"]["published_hint_basis"] = "xml_publication"
    assert publication_outside(job)["status"] == "outside_window"
    assert publication_outside(job)["metrics"]["avoided_article_requests"] == 1


def test_atom_updated_is_not_publication():
    from buscador_midias.catalog.mechanisms.parsing import parse_document

    xml = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        b'<link href="https://jose.example/a"/><updated>2026-10-24T00:00:00Z</updated>'
        b"<published>2020-01-01T00:00:00Z</published></entry><entry>"
        b'<link href="https://jose.example/b"/><updated>2026-10-24T00:00:00Z</updated>'
        b"</entry></feed>"
    )
    doc = parse_document(xml, "https://jose.example/feed")
    assert doc["dates"]["https://jose.example/a"].startswith("2020-")
    assert "https://jose.example/b" not in doc["dates"]
    assert doc["hints"]["https://jose.example/b"].startswith("2026-")


def test_large_document_continues_without_repeating_http(ingest):
    ingest.create_run({})
    job = next_job(ingest, "discovery")
    plan = dict(
        candidates=[dict(url=f"https://jose.example/news/{i}") for i in range(1100)],
        children=[],
        metrics=dict(requests=1),
        outcome="urls_encontradas",
    )
    key = ingest.save_plan(job, plan)
    ingest.finish(job, Engine.discovery_slice(job, plan, key))
    engine = Engine(ingest, reader_factory=lambda *args: pytest.fail("Não deve repetir HTTP"))
    for _ in range(2):
        continuation = next_job(ingest, "discovery")
        ingest.finish(continuation, engine.discovery(continuation))
    with ingest.connection() as db:
        assert db.execute("SELECT count(*) n FROM ingest_articles").fetchone()["n"] == 1100
        assert db.execute("SELECT count(*) n FROM ingest_document_plans").fetchone()["n"] == 0
        assert (
            db.execute("SELECT count(*) n FROM ingest_jobs WHERE stage='discovery'").fetchone()["n"]
            == 3
        )


def mapped_samples():
    return [
        dict(
            url=f"https://jose.example/sitemap/2026/10/{day}_1.xml",
            sample=dict(
                url=f"https://jose.example/politica/noticia/2026/10/{day}/n{i}",
                text=True,
                published_at=f"2026-10-{day}T10:00:00+00:00",
            ),
        )
        for day in ("10", "11")
        for i in range(3)
    ]


def test_temporal_route_requires_real_mapped_publications():
    from buscador_midias.ingestion.routes import learn

    rows = mapped_samples()
    rules = learn(rows)
    assert {r["kind"] for r in rules} == {"article", "document"}
    assert learn(rows[:2]) == []
    rows.append(
        dict(
            url=rows[0]["url"], sample=dict(rows[0]["sample"], published_at="2026-01-01T00:00:00Z")
        )
    )
    assert learn(rows) == []


def test_learned_route_can_be_disabled_and_has_overlap(ingest):
    import time

    from buscador_midias.ingestion.routes import Routes, learn

    ingest.create_run({})
    job = next_job(ingest, "discovery")
    router = Routes(ingest)
    router.cache[1] = (time.monotonic() + 300, learn(mapped_samples()))
    old = "https://jose.example/sitemap/2026/10/10_1.xml"
    result = router.outside(job, old, "document")
    assert result and result["routing"]["inferred"]
    assert "published_at" not in result["article"]
    assert router.outside(job, "https://jose.example/unknown/2026/10/10_1.xml", "document") is None
    job["snapshot"]["parameters"]["learn_temporal_routes"] = 0
    assert router.outside(job, old, "document") is None


def test_backpressure_leaves_final_consumers_available(ingest, monkeypatch):
    from buscador_midias.ingestion.flow import BACKLOG_LIMITS

    monkeypatch.setitem(BACKLOG_LIMITS, "context", 2)
    run = ingest.create_run({})
    with ingest.connection() as db:
        poll = db.execute("SELECT id FROM ingest_polls").fetchone()["id"]
        for i in range(2):
            ingest.enqueue(db, "context", str(i), run["id"], poll, {})
    assert not ingest.capacity("fetch")[0]
    assert ingest.capacity("persist")[0]


def test_existing_backlog_is_filtered_without_http_or_stale_publication(ingest, monkeypatch):
    ingest.create_run({})
    discovery = next_job(ingest, "discovery")
    ingest.finish(
        discovery,
        dict(
            candidates=[
                dict(
                    url="https://jose.example/old",
                    published_hint="2020-01-01T00:00:00Z",
                    published_hint_basis="xml_publication",
                )
            ]
        ),
    )
    service = Service(ingest)
    cursor, count = service.filter_window_once()
    assert cursor and count == 1
    monkeypatch.setattr(
        "buscador_midias.ingestion.broker.publish",
        lambda *a, **kw: pytest.fail("Tarefa já concluída não deve ser publicada"),
    )
    assert service.publish_once(None, "dedup") == 0
    with ingest.connection() as db:
        article = db.execute("SELECT status FROM ingest_articles").fetchone()
        event = db.execute(
            "SELECT published_at,discarded_at FROM ingest_outbox WHERE stage='dedup'"
        ).fetchone()
        assert article["status"] == "outside_window"
        assert event["published_at"] is None and event["discarded_at"] is not None


def test_document_slices_keep_all_children_and_candidates():
    job = dict(payload={})
    plan = dict(candidates=list(range(1100)), children=list(range(200)), metrics={})
    entries, children = [], []
    for cursor in range(4):
        job["payload"]["cursor"] = cursor
        output = Engine.discovery_slice(job, plan, "key")
        entries.extend(output["candidates"])
        children.extend(output["children"])
        assert bool(output.get("continuation")) == (cursor < 3)
    assert entries == list(range(1100))
    assert children == list(range(200))


def test_heartbeat_failure_does_not_kill_worker_or_hide_checkpoint_error(ingest, monkeypatch):
    service = Service(ingest)

    def unavailable(*args, **kwargs):
        raise OSError("telemetria indisponível")

    monkeypatch.setattr(ingest, "heartbeat", unavailable)
    service.heartbeat("worker", "fetch", "online")
    ingest.create_run({})
    with ingest.connection() as db:
        ident = db.execute("SELECT id FROM ingest_jobs").fetchone()["id"]
    monkeypatch.setattr(service.engine, "execute", lambda job: dict(outcome="sem_novidades"))
    assert service.process(ident, "worker")
    with ingest.connection() as db:
        assert (
            db.execute("SELECT state FROM ingest_jobs WHERE id=%s", (ident,)).fetchone()["state"]
            == "succeeded"
        )


def test_recovery_is_published_and_claimed_only_on_its_lane(ingest, monkeypatch):
    """Falhas saem da fila principal sem perder tarefa, histórico ou orçamento."""
    from buscador_midias.ingestion import broker

    ingest.create_run({})
    job = next_job(ingest, "discovery")
    ingest.failure(job, "HTTP 503")
    with ingest.connection() as db:
        db.execute("UPDATE ingest_jobs SET available_at=now() WHERE id=%s", (job["id"],))
        db.execute("UPDATE ingest_outbox SET ready_at=now()")
    sent = []
    monkeypatch.setattr(broker, "publish", lambda *args, **kw: sent.append(kw))
    service = Service(ingest)
    assert service.publish_once(None, "discovery") == 0
    assert service.publish_once(None, "discovery", recovery=True) == 1
    assert sent == [dict(failed=False, recovery=True)]
    assert ingest.claim(job["id"], "main", recovery=False) is None
    recovered = ingest.claim(job["id"], "recovery", recovery=True)
    assert recovered["failures"] == 1
    assert recovered["attempts"] == 2
    ingest.finish(recovered, {})
    assert service.publish_once(None, "discovery", recovery=True) == 0


def test_provider_deferral_is_recovery_without_spending_retry_budget(ingest, monkeypatch):
    from buscador_midias.ingestion import broker

    ingest.create_run({})
    job = next_job(ingest, "discovery")
    ingest.failure(job, "Provedor indisponível", busy=True, recovery=True)
    counts = ingest.overview()["runs"][0]
    assert counts["primary_pending"] == 0
    assert counts["recovery_pending"] == 1
    with ingest.connection() as db:
        db.execute("UPDATE ingest_jobs SET available_at=now() WHERE id=%s", (job["id"],))
        db.execute("UPDATE ingest_outbox SET ready_at=now()")
    monkeypatch.setattr(broker, "publish", lambda *args, **kw: None)
    assert Service(ingest).publish_once(None, "discovery", recovery=True) == 1
    recovered = ingest.claim(job["id"], "recovery", recovery=True)
    assert recovered["failures"] == 0
    ingest.failure(recovered, "Host ocupado", busy=True)
    with ingest.connection() as db:
        row = db.execute("SELECT payload FROM ingest_jobs WHERE id=%s", (job["id"],)).fetchone()
        assert row["payload"]["recovery"] is True


def test_recovery_backlog_does_not_throttle_primary_producers(ingest, monkeypatch):
    from buscador_midias.ingestion.flow import BACKLOG_LIMITS

    monkeypatch.setitem(BACKLOG_LIMITS, "fetch", 1)
    ingest.create_run({})
    ingest.finish(
        next_job(ingest, "discovery"), dict(candidates=[dict(url="https://jose.example/a")])
    )
    ingest.finish(next_job(ingest, "dedup"), dict(next="fetch"))
    assert not ingest.capacity("dedup")[0]
    job = next_job(ingest, "fetch")
    ingest.failure(job, "HTTP 503")
    assert ingest.capacity("dedup")[0]


def test_ingestion_disables_paid_fallback_and_limits_attempt_configuration(ingest):
    setting = ingest.settings()
    assert setting["config"]["bright_data"] is False
    for patch in (dict(bright_data=True), dict(max_attempts=4)):
        with pytest.raises(CatalogError):
            ingest.configure(dict(version=setting["version"], config=patch))


def test_old_config_cannot_exceed_three_failed_attempts(ingest):
    from psycopg.types.json import Jsonb

    run = ingest.create_run({})
    with ingest.connection() as db:
        db.execute(
            "UPDATE ingest_runs SET config=config||%s WHERE id=%s",
            (Jsonb(dict(max_attempts=10, bright_data=True)), run["id"]),
        )
    for attempt in range(3):
        job = next_job(ingest, "discovery")
        with Engine(ingest).reader(job, ["https://jose.example"]) as reader:
            from buscador_midias.catalog.reader import TestReader

            assert type(reader) is TestReader
        assert ingest.failure(job, "HTTP 503") == ("failed" if attempt == 2 else "retry")
        with ingest.connection() as db:
            db.execute("UPDATE ingest_jobs SET available_at=now() WHERE id=%s", (job["id"],))
    assert ingest.claim(job["id"], "fourth") is None


def test_worker_configuration_accepts_requested_concurrency(ingest):
    setting = ingest.settings()
    workers = dict(setting["config"]["workers"], discovery=9, fetch=18, context=18)
    updated = ingest.configure(dict(version=setting["version"], config=dict(workers=workers)))
    assert updated["config"]["workers"] == workers
    with pytest.raises(CatalogError):
        ingest.configure(
            dict(version=updated["version"], config=dict(workers=dict(workers, fetch=33)))
        )


def seed_unbalanced_discovery(ingest):
    """Mil e uma URLs de um site antes de uma URL de outro site."""
    run = ingest.create_run({})
    root = next_job(ingest, "discovery")
    ingest.finish(root, {})
    with ingest.connection() as db:
        db.execute(
            "INSERT INTO ingest_jobs(id,stage,subject,run_id,poll_id,payload) "
            "SELECT md5('fair-'||n),'discovery','fair-'||n,%s,%s,"
            "jsonb_build_object('url',CASE WHEN n<=1001 THEN "
            "'https://jose.example/map-'||n ELSE 'https://other.example/map' END) "
            "FROM generate_series(1,1002) n",
            (run["id"], root["poll_id"]),
        )
        db.execute(
            "INSERT INTO ingest_outbox(job_id,dispatch,stage,ready_at) "
            "SELECT id,dispatch,stage,now()-interval '1 hour' FROM ingest_jobs "
            "WHERE subject LIKE 'fair-%%' ORDER BY substring(subject from 6)::int"
        )
    return root


def test_network_admission_sees_hosts_beyond_first_thousand_jobs(ingest, monkeypatch):
    from buscador_midias.ingestion import broker

    seed_unbalanced_discovery(ingest)
    sent = []
    monkeypatch.setattr(broker, "publish", lambda ch, stage, ident, **kw: sent.append(ident))
    assert Service(ingest).publish_once(None, "discovery") == 2
    with ingest.connection() as db:
        urls = db.execute(
            "SELECT payload->>'url' url FROM ingest_jobs WHERE id=ANY(%s)", (sent,)
        ).fetchall()
    assert {u["url"].split("/")[2] for u in urls} == {"jose.example", "other.example"}


def test_network_admission_excludes_busy_hosts_before_limiting(ingest, monkeypatch):
    from buscador_midias.ingestion import broker

    seed_unbalanced_discovery(ingest)
    with ingest.connection() as db:
        db.execute("UPDATE ingest_jobs SET state='queued' WHERE subject='fair-1'")
    sent = []
    monkeypatch.setattr(broker, "publish", lambda ch, stage, ident, **kw: sent.append(ident))
    assert Service(ingest).publish_once(None, "discovery") == 1
    with ingest.connection() as db:
        url = db.execute(
            "SELECT payload->>'url' url FROM ingest_jobs WHERE id=%s", (sent[0],)
        ).fetchone()["url"]
    assert url == "https://other.example/map"


def test_network_admission_rotates_hosts_before_repeating_large_source(ingest, monkeypatch):
    from types import SimpleNamespace

    from buscador_midias.ingestion import broker
    from buscador_midias.ingestion.flow import QUEUE_LIMITS

    seed_unbalanced_discovery(ingest)
    monkeypatch.setitem(QUEUE_LIMITS, "discovery", 1)
    channel = SimpleNamespace(
        queue_declare=lambda **kw: SimpleNamespace(method=SimpleNamespace(message_count=0))
    )
    sent = []
    monkeypatch.setattr(broker, "publish", lambda ch, stage, ident, **kw: sent.append(ident))
    service = Service(ingest)
    assert service.publish_once(channel, "discovery") == 1
    ingest.finish(ingest.claim(sent[0], "test"), {})
    assert service.publish_once(channel, "discovery") == 1
    with ingest.connection() as db:
        urls = [
            db.execute(
                "SELECT payload->>'url' url FROM ingest_jobs WHERE id=%s", (ident,)
            ).fetchone()["url"]
            for ident in sent
        ]
    assert urls == ["https://jose.example/map-1", "https://other.example/map"]


def test_retry_after_is_durable_and_blocks_other_jobs_of_same_host(ingest, monkeypatch):
    from buscador_midias.ingestion import broker

    ingest.store.save(
        "endpoint", dict(url="https://jose.example/another", kind="feed"), source_id=1
    )
    source = ingest.store.save("source", dict(name="Outra fonte", url="https://another.example"))
    ingest.store.save(
        "endpoint", dict(url="https://another.example/feed", kind="feed"), source_id=source["id"]
    )
    run = ingest.create_run({})
    job = next_job(ingest, "discovery")
    assert job["snapshot"]["source"]["id"] == 1
    assert (
        ingest.failure(
            job, "HTTP 429", metrics=dict(retry_after_seconds=120, retry_after_host="jose.example")
        )
        == "retry"
    )
    with ingest.connection() as db:
        cooldown = db.execute(
            "SELECT extract(epoch FROM available_at-now()) seconds FROM "
            "ingest_host_cooldowns WHERE host='jose.example'"
        ).fetchone()
        pending = db.execute(
            "SELECT extract(epoch FROM available_at-now()) seconds,failures "
            "FROM ingest_jobs WHERE id=%s",
            (job["id"],),
        ).fetchone()
        assert cooldown["seconds"] >= 119 and pending["seconds"] >= 119
        assert pending["failures"] == 1
    published = []
    monkeypatch.setattr(broker, "publish", lambda ch, stage, ident, **kw: published.append(ident))
    assert Service(ingest).publish_once(None, "discovery") == 1
    with ingest.connection() as db:
        task = db.execute("SELECT payload FROM ingest_jobs WHERE id=%s", (published[0],)).fetchone()
    assert task["payload"]["url"] == "https://another.example/feed"
    # Tarefas já publicadas também conferem o prazo antes de acessar o host.
    from buscador_midias.ingestion.engine import HostBusyError

    with (
        pytest.raises(HostBusyError, match="Retry-After"),
        Engine(ingest).reader(job, ["https://jose.example"]) as reader,
    ):
        reader.before_request("jose.example")
    # A rodada e a tarefa original continuam preservadas, sem novas tentativas físicas.
    with ingest.connection() as db:
        assert db.execute("SELECT count(*) n FROM ingest_runs").fetchone()["n"] == 1
        assert (
            db.execute("SELECT state FROM ingest_runs WHERE id=%s", (run["id"],)).fetchone()[
                "state"
            ]
            == "running"
        )


@pytest.mark.parametrize(
    "status,error", [(404, None), (403, None), (None, "redirect_cycle: ciclo")]
)
def test_permanent_http_failure_is_terminal_on_first_attempt(ingest, status, error):
    from buscador_midias.ingestion.engine import RetryableError

    ingest.create_run({})
    job = next_job(ingest, "discovery")
    reader = type("Reader", (), dict(requests=1))()
    with pytest.raises(RetryableError) as caught:
        Engine(ingest).http_failure(
            reader, FetchResult("https://jose.example/feed", status, b"", error), "no XML"
        )
    failure = caught.value
    assert not failure.retryable
    assert (
        ingest.failure(job, str(failure), metrics=failure.metrics, retryable=failure.retryable)
        == "failed"
    )
    with ingest.connection() as db:
        saved = db.execute(
            "SELECT failures,state FROM ingest_jobs WHERE id=%s", (job["id"],)
        ).fetchone()
        assert saved == dict(failures=1, state="failed")
        assert (
            db.execute(
                "SELECT count(*) n FROM ingest_outbox WHERE job_id=%s AND kind='failed'",
                (job["id"],),
            ).fetchone()["n"]
            == 1
        )


def seed_recovery_hosts(ingest):
    run = ingest.create_run({})
    root = next_job(ingest, "discovery")
    ingest.finish(root, {})
    urls = [
        "https://a.example/1",
        "https://a.example/2",
        "https://b.example/1",
        "https://c.example/1",
        "https://d.example/1",
    ]
    jobs = []
    with ingest.connection() as db:
        for url in urls:
            ident = ingest.enqueue(db, "discovery", url, run["id"], root["poll_id"], {"url": url})
            db.execute("UPDATE ingest_jobs SET state='retry',failures=1 WHERE id=%s", (ident,))
            jobs.append(ident)
    return run, root, jobs


def test_recovery_publication_scales_but_respects_host_and_queue_limits(ingest, monkeypatch):
    from types import SimpleNamespace

    from buscador_midias.ingestion import broker

    _, _, jobs = seed_recovery_hosts(ingest)
    config = ingest.settings()
    ingest.configure(
        dict(
            version=config["version"],
            config=dict(workers=dict(config["config"]["workers"], discovery=3)),
        )
    )
    sent = []
    monkeypatch.setattr(broker, "publish", lambda ch, stage, ident, **kw: sent.append(ident))
    channel = SimpleNamespace(
        queue_declare=lambda **kw: SimpleNamespace(method=SimpleNamespace(message_count=1))
    )
    # Três slots configurados e uma mensagem já no Rabbit: publicar no máximo duas.
    assert Service(ingest).publish_once(channel, "discovery", recovery=True) == 2
    with ingest.connection() as db:
        urls = [
            r["url"]
            for r in db.execute(
                "SELECT payload->>'url' url FROM ingest_jobs WHERE id=ANY(%s)", (sent,)
            ).fetchall()
        ]
    assert len({u.split("/")[2] for u in urls}) == 2
    assert set(sent).issubset(jobs)


def test_recovery_keeps_future_cooldown_and_primary_hosts_unpublished(ingest, monkeypatch):
    from buscador_midias.ingestion import broker

    run, root, jobs = seed_recovery_hosts(ingest)
    with ingest.connection() as db:
        ingest.enqueue(
            db, "discovery", "fresh-a", run["id"], root["poll_id"], {"url": "https://a.example/new"}
        )
        db.execute(
            "UPDATE ingest_outbox SET ready_at=now()+interval '1 hour' WHERE job_id=%s", (jobs[2],)
        )
        db.execute("INSERT INTO ingest_host_cooldowns VALUES('c.example',now()+interval '1 hour')")
    sent = []
    monkeypatch.setattr(broker, "publish", lambda ch, stage, ident, **kw: sent.append(ident))
    # a: tem tarefa nova; b: prazo futuro; c: Retry-After; apenas d está elegível.
    assert Service(ingest).publish_once(None, "discovery", recovery=True) == 1
    assert sent == [jobs[4]]
    service = Service(ingest)
    assert service.publish_once(None, "discovery") == 1
    with ingest.connection() as db:
        states = {
            r["id"]: r["state"]
            for r in db.execute(
                "SELECT id,state FROM ingest_jobs WHERE id=ANY(%s)", (jobs,)
            ).fetchall()
        }
    assert all(states[j] == "retry" for j in jobs[:4])


def _daily(ingest):
    from buscador_midias.ingestion.flow import today_start

    config = ingest.settings()
    ingest.configure(dict(version=config["version"], config=dict(window_mode="today")))
    return today_start(datetime.now(UTC))


def test_manual_round_for_a_past_day_covers_all_of_that_day(ingest):
    from datetime import timedelta

    midnight = _daily(ingest)
    run = ingest.create_run(dict(day=(midnight - timedelta(days=1)).date().isoformat()))
    assert run["since"] == midnight - timedelta(days=1)
    assert run["until"] == midnight


def test_manual_round_for_the_current_day_runs_from_midnight_until_now(ingest):
    midnight = _daily(ingest)
    run = ingest.create_run(dict(day=midnight.date().isoformat()))
    assert run["since"] == midnight and run["until"] > midnight


@pytest.mark.parametrize(
    "extra",
    [
        dict(day="2999-01-01"),
        dict(day="ontem"),
        dict(day="2026-10-24", since="2026-10-24T00:00:00-03:00"),
    ],
)
def test_manual_round_by_day_rejects_invalid_or_mixed_options(ingest, extra):
    with pytest.raises(CatalogError):
        ingest.create_run(extra)
    with ingest.connection() as db:
        assert db.execute("SELECT count(*) n FROM ingest_runs").fetchone()["n"] == 0


def test_evidence_backlog_does_not_pause_context(ingest):
    run = ingest.create_run({})
    with ingest.connection() as db:
        poll = db.execute("SELECT id FROM ingest_polls").fetchone()["id"]
        for i in range(205):
            ingest.enqueue(db, "evidence", f"evidence-backlog-{i}", run["id"], poll, {})
    assert ingest.capacity("context") == (True, "")
    with ingest.connection() as db:
        assert (
            db.execute("SELECT count(*) n FROM ingest_jobs WHERE stage='evidence'").fetchone()["n"]
            == 205
        )
