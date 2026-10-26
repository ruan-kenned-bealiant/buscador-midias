"""Procedência e deduplicação dos candidatos de um diretório externo."""

import importlib
from pathlib import Path

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest as ingest_fixture

ingest = ingest_fixture


@pytest.fixture
def feeder(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("map_feeder_candidates")


def test_extrai_url_real_e_nao_confunde_feed_gerado_com_rss(feeder):
    body = b"""<html><span class="discover-feeds__text">
    <a>World</a><small>https://rss.example.com/world?x=1&amp;y=2</small>
    <small class="discover-feeds__meta">Last post today</small></span>
    <span class="discover-feeds__text"><a>Homepage</a>
    <small>Feeder builds this feed from https://example.com/</small></span>
    <span class="discover-feeds__text"><a>Older</a><small>http://rss.example.com/world?x=1&amp;y=2</small></span></html>"""
    rows = feeder.extract_candidates(body, {"name": "Example", "url": "https://example.com"})
    assert len(rows) == 1
    assert rows[0]["url"] == "https://rss.example.com/world?x=1&y=2"
    assert rows[0]["origin"] == "publisher_domain"


def test_nao_trata_dominio_parecido_nem_agregador_como_oficial(feeder):
    source = {"name": "Reuters", "url": "https://www.reuters.com"}
    assert feeder.feed_origin("https://feeds.reuters.com/rss", source) == "publisher_domain"
    assert feeder.feed_origin("https://reuters.com.evil.example/rss", source) == "third_party"
    assert (
        feeder.feed_origin("https://news.google.com/rss/search?q=site:reuters.com", source)
        == "aggregator"
    )
    assert feeder.feed_origin("https://user:pass@reuters.com/feed", source) == "invalid"
    assert (
        feeder.feed_origin(
            "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
            {"name": "WSJ", "url": "https://www.wsj.com"},
        )
        == "known_distributor"
    )


def test_preserva_duplicatas_e_feeds_de_terceiros_sem_ativacao(feeder):
    source = {"id": 1, "name": "Example", "url": "https://example.com"}
    known = dict(
        id=7,
        source_id=1,
        url="http://example.com/rss",
        deleted_at=None,
        active=1,
        last_test={"usable_for_news": True},
    )
    candidate = dict(
        url="https://example.com/rss", label="News", origin="publisher_domain", feeder_trouble=False
    )
    other = dict(
        url="https://news.google.com/rss", label="Other", origin="aggregator", feeder_trouble=False
    )
    rows = feeder.candidates_for(
        source,
        {
            "provenance": "https://feeder.co/discover/site/example.com",
            "candidates": [candidate, other],
        },
        {"links": [known]},
    )
    assert {r["decision"] for r in rows} == {"already_functional", "third_party_review"}


@pytest.mark.parametrize("manual", [False, True])
def test_aplicacao_preserva_exclusao_manual_e_audita_restauracao(
    feeder, monkeypatch, tmp_path, request, manual
):
    import hashlib
    import os
    from contextlib import contextmanager

    if os.environ.get("BM_TEST_POSTGRES") != "1":
        pytest.skip("PostgreSQL de teste explícito")
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from test_transport_postgres import fixture_row

    from buscador_midias.catalog.postgres import connect_postgres

    ingestion = request.getfixturevalue("ingest")
    with connect_postgres() as db:
        db.row_factory = dict_row
        db.execute(f"SET LOCAL search_path TO {ingestion.store.schema},pg_catalog")
        db.execute("UPDATE links SET active=0,deleted_at=now(),version=version+1 WHERE id=1")
        version = db.execute("SELECT version FROM links WHERE id=1").fetchone()["version"]
        db.execute(
            "INSERT INTO events(entity,entity_id,action,happened_at,after_json) "
            "VALUES('endpoint',1,%s,now(),%s)",
            ("delete" if manual else "validation_exclude", Jsonb({"version": version})),
        )
        wrapped = fixture_row(ingestion, db)
        source = db.execute("SELECT * FROM sites WHERE id=1").fetchone()
        links = db.execute("SELECT * FROM links").fetchall()
    out = tmp_path / "audit"
    body = b"<html><body>prova com hash preservada</body></html>"
    folder = out / "evidence/midias-ia/evidencias"
    folder.mkdir(parents=True)
    (folder / "proof.html").write_bytes(body)
    proof = dict(
        status="captured",
        path="midias-ia/evidencias/proof.html",
        sha256=hashlib.sha256(body).hexdigest(),
        url=wrapped["confirmation"]["url"],
    )
    candidate = dict(
        url=wrapped["link"]["url"],
        label="RSS",
        origin="publisher_domain",
        provenance="https://feeder.co/discover/site/a.com",
    )
    row = dict(
        source=source,
        results=[
            dict(
                confirmed=True,
                candidate=candidate,
                result=wrapped["result"],
                profile="curl_chrome_h2",
                evidence=[proof],
            )
        ],
    )

    class Connection:
        def __init__(self, db):
            self.db = db

        def execute(self, query, params=None):
            return self.db.execute(query.replace("midias_manager", ingestion.store.schema), params)

    @contextmanager
    def isolated(_credentials):
        with connect_postgres() as db:
            db.row_factory = dict_row
            yield Connection(db)

    monkeypatch.setattr(feeder, "connect_postgres", isolated)
    monkeypatch.setattr(feeder, "OUT", out)
    decisions = feeder.apply_source(row, dict(links=links))
    with connect_postgres() as db:
        db.row_factory = dict_row
        db.execute(f"SET LOCAL search_path TO {ingestion.store.schema},pg_catalog")
        link = db.execute("SELECT * FROM links WHERE id=1").fetchone()
        if manual:
            assert decisions[0]["action"] == "manual_deletion_preserved"
            assert link["deleted_at"] is not None and not link["active"]
        else:
            assert decisions[0]["action"] == "feeder_restore"
            assert link["active"] and link["deleted_at"] is None
            assert link["access_transport"] == "curl_chrome_h2"
            assert link["last_test"]["evidence_capture"]["sha256"] == proof["sha256"]
            assert (
                db.execute(
                    "SELECT count(*) n FROM events WHERE action='feeder_restore'"
                ).fetchone()["n"]
                == 1
            )
