# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Barreiras de conteúdo rotineiro no fluxo: URL, título e categoria da análise de contexto."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_catalog_testing import FakeReader, article
from test_ingestion import Analyzer, ingest, next_job  # noqa: F401

from buscador_midias.catalog.store import CatalogError
from buscador_midias.evidence.storage import LocalEvidenceStore
from buscador_midias.ingestion.artifacts import Artifacts
from buscador_midias.ingestion.engine import Engine
from buscador_midias.ingestion.store import IngestStore
from buscador_midias.net.fetcher import FetchResult

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)
TITLE = "Reportagem sobre os acontecimentos na cidade"


def configure(repo, **config):
    repo.configure(dict(version=repo.settings()["version"], config=config))


def make_reader(path="/noticia", title=None, requested=None):
    """Feed com um único artigo em ``path``; ``requested`` registra os downloads de matéria."""
    html = article().replace(
        b"A reportagem ouviu", "João Silva participou. A reportagem ouviu".encode()
    )
    if title:
        html = html.replace(TITLE.encode(), title.encode())

    def factory(roots, progress):
        base = roots[0].rstrip("/")

        class Reader(FakeReader):
            def get(self, url):
                if url.startswith(base + path) and requested is not None:
                    requested.append(url)
                return super().get(url)

        return Reader(
            {
                roots[
                    1
                ]: f"<rss><channel><item><link>{base}{path}</link></item></channel></rss>".encode(),
                base + path: FetchResult(base + path, 200, html, None, base + path, "text/html"),
            },
            progress,
        )

    return factory


def analyzer(category=None, finding=False):
    class Classified(Analyzer):
        def analyze(self, items):
            return [replace(super().analyze(items)[0], content_category=category, finding=finding)]

    return Classified


def engine_for(ingest, tmp_path, monkeypatch, reader_factory, analyzer_factory=Analyzer):
    engine = Engine(
        ingest,
        artifacts=Artifacts(tmp_path / "work"),
        reader_factory=reader_factory,
        analyzer_factory=analyzer_factory,
        evidence_store=LocalEvidenceStore(tmp_path / "evidence"),
    )
    monkeypatch.setattr(engine, "dedup", lambda job: dict(next="fetch", outcome="nova_url"))
    return engine


def run_stages(ingest, engine, stages):
    for stage in stages:
        job = next_job(ingest, stage)
        ingest.finish(job, engine.execute(job))
    ingest.maintain()


def article_row(ingest):
    with ingest.connection() as db:
        return db.execute("SELECT * FROM ingest_articles").fetchone()


def pending_stages(ingest):
    with ingest.connection() as db:
        return {
            r["stage"]
            for r in db.execute(
                "SELECT stage FROM ingest_jobs WHERE state IN ('pending','queued','retry')"
            )
        }


def test_defaults_are_filled_for_installations_that_predate_the_filter(ingest):
    with ingest.connection() as db:
        db.execute(
            "UPDATE ingest_settings SET config=config-'content_filter'-'blocked_content_categories'"
        )
    config = IngestStore(ingest.store).settings()["config"]
    assert config["content_filter"] == "shadow"
    assert config["blocked_content_categories"] == ["horoscope", "weather", "cooking", "sports"]


@pytest.mark.parametrize(
    "patch",
    [
        dict(content_filter="agressivo"),
        dict(content_filter=True),
        dict(blocked_content_categories="sports"),
        dict(blocked_content_categories=["news"]),
        dict(blocked_content_categories=["opinion"]),
        dict(blocked_content_categories=["sports", "sports"]),
        dict(blocked_content_categories=["futebol"]),
    ],
)
def test_filter_settings_are_validated(ingest, patch):
    with pytest.raises(CatalogError):
        configure(ingest, **patch)


def test_filter_settings_accept_valid_values(ingest):
    configure(ingest, content_filter="enforce", blocked_content_categories=["sports", "lifestyle"])
    config = ingest.settings()["config"]
    assert config["content_filter"] == "enforce"
    assert config["blocked_content_categories"] == ["sports", "lifestyle"]
    configure(ingest, blocked_content_categories=[])


def test_context_barrier_stops_routine_category_before_evidence(ingest, tmp_path, monkeypatch):
    configure(ingest, allow_undated=True)
    engine = engine_for(ingest, tmp_path, monkeypatch, make_reader(), analyzer("sports"))
    ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    run_stages(ingest, engine, ["discovery", "dedup", "fetch", "context"])
    row = article_row(ingest)
    assert row["status"] == "excluded_content"
    assert row["payload"]["exclusion"] == dict(category="sports", rule="ai_category", stage="ai")
    assert row["payload"]["analysis"]["content_category"] == "sports"
    assert not row["payload"].get("body")
    assert "evidence" not in pending_stages(ingest)
    assert not (tmp_path / "work" / (row["hash"] + ".html.gz")).exists()
    with ingest.connection() as db:
        outcome = db.execute("SELECT outcome FROM ingest_jobs WHERE stage='context'").fetchone()[
            "outcome"
        ]
    assert outcome == "categoria_sem_interesse"


def test_context_barrier_keeps_routine_category_with_adverse_finding(ingest, tmp_path, monkeypatch):
    configure(ingest, allow_undated=True)
    engine = engine_for(ingest, tmp_path, monkeypatch, make_reader(), analyzer("sports", True))
    ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    run_stages(ingest, engine, ["discovery", "dedup", "fetch", "context"])
    assert article_row(ingest)["status"] != "excluded_content"
    assert "evidence" in pending_stages(ingest)


def test_context_barrier_can_be_disabled_with_an_empty_list(ingest, tmp_path, monkeypatch):
    configure(ingest, allow_undated=True, blocked_content_categories=[])
    engine = engine_for(ingest, tmp_path, monkeypatch, make_reader(), analyzer("sports"))
    ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    run_stages(ingest, engine, ["discovery", "dedup", "fetch", "context"])
    assert "evidence" in pending_stages(ingest)


def test_enforced_url_rule_skips_the_download(ingest, tmp_path, monkeypatch):
    configure(ingest, allow_undated=True, content_filter="enforce")
    requested: list[str] = []
    reader = make_reader("/horoscopo/libra", requested=requested)
    engine = engine_for(ingest, tmp_path, monkeypatch, reader)
    ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    run_stages(ingest, engine, ["discovery", "dedup", "fetch"])
    row = article_row(ingest)
    assert row["status"] == "excluded_content"
    assert row["payload"]["exclusion"]["category"] == "horoscope"
    assert row["payload"]["exclusion"]["stage"] == "url"
    assert requested == []
    assert "context" not in pending_stages(ingest)


def test_enforced_title_rule_stops_after_extraction_and_keeps_no_artifact(
    ingest, tmp_path, monkeypatch
):
    configure(ingest, allow_undated=True, content_filter="enforce")
    reader = make_reader(title="Horóscopo do dia: previsão para os 12 signos")
    engine = engine_for(ingest, tmp_path, monkeypatch, reader)
    ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    run_stages(ingest, engine, ["discovery", "dedup", "fetch"])
    row = article_row(ingest)
    assert row["status"] == "excluded_content"
    assert row["payload"]["exclusion"]["stage"] == "title"
    assert not row["payload"].get("body")
    assert "context" not in pending_stages(ingest)
    assert not (tmp_path / "work" / (row["hash"] + ".html.gz")).exists()


def test_shadow_mode_records_the_rule_but_keeps_processing(ingest, tmp_path, monkeypatch):
    configure(ingest, allow_undated=True, content_filter="shadow")
    reader = make_reader(title="Horóscopo do dia: previsão para os 12 signos")
    engine = engine_for(ingest, tmp_path, monkeypatch, reader)
    ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    run_stages(ingest, engine, ["discovery", "dedup", "fetch"])
    row = article_row(ingest)
    assert row["status"] != "excluded_content"
    assert row["payload"]["shadow_exclusion"]["category"] == "horoscope"
    assert "context" in pending_stages(ingest)


def test_off_mode_ignores_the_rules(ingest, tmp_path, monkeypatch):
    configure(ingest, allow_undated=True, content_filter="off")
    reader = make_reader("/horoscopo/libra", title="Horóscopo do dia: previsão para os 12 signos")
    engine = engine_for(ingest, tmp_path, monkeypatch, reader)
    ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    run_stages(ingest, engine, ["discovery", "dedup", "fetch"])
    row = article_row(ingest)
    assert "shadow_exclusion" not in row["payload"] and "exclusion" not in row["payload"]
    assert "context" in pending_stages(ingest)


def test_adverse_title_is_never_excluded_even_when_enforced(ingest, tmp_path, monkeypatch):
    configure(ingest, allow_undated=True, content_filter="enforce")
    reader = make_reader(title="Horóscopo: astrólogo é preso por estelionato")
    engine = engine_for(ingest, tmp_path, monkeypatch, reader)
    ingest.create_run(dict(since="1990-01-01T00:00:00+00:00"))
    run_stages(ingest, engine, ["discovery", "dedup", "fetch"])
    assert article_row(ingest)["status"] != "excluded_content"
    assert "context" in pending_stages(ingest)
