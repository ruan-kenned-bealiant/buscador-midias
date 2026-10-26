import importlib
import os
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_catalog_postgres import local, schema  # noqa: F401
from test_catalog_testing import FakeReader, article
from test_ingestion import ingest as ingest_fixture

from buscador_midias.catalog.postgres import connect_postgres
from buscador_midias.catalog.testing import run_test
from buscador_midias.ingestion.engine import Engine

ingest = ingest_fixture

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="PostgreSQL de teste explícito"
)


@pytest.fixture
def applier(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("apply_transport_recovery")


def fixture_row(ingest, db, ident=1):
    link = db.execute("SELECT * FROM links WHERE id=%s", (ident,)).fetchone()
    source = db.execute("SELECT * FROM sites WHERE id=%s", (link["source_id"],)).fetchone()
    model = ingest.store.model_settings("rss")
    url = source["url"] + "/noticia"
    feed = f"<rss><channel><item><link>{url}</link></item></channel></rss>".encode()
    config = dict(
        test_id=f"proof-{ident}",
        url=link["url"],
        kind="feed",
        model="rss",
        model_version=model["version"],
        access_transport="curl_chrome_h2",
    )

    def factory(roots, progress):
        r = FakeReader({link["url"]: feed, url: article()}, progress)
        original = r.get

        def get(target):
            from buscador_midias.net.fetcher import FetchResult

            response = original(target)
            return FetchResult(
                target,
                200,
                response.content,
                None,
                target,
                "text/html" if target == url else "application/rss+xml",
            )

        r.get = get
        return r

    result = run_test(
        config,
        dict(id=source["id"], name=source["name"], url=source["url"]),
        lambda _: None,
        factory,
    )
    assert result["usable_for_news"]
    item = dict(
        id=ident,
        url=link["url"],
        version=link["version"],
        source_id=source["id"],
        source_name=source["name"],
        source_url=source["url"],
        source_version=source["version"],
    )
    return dict(
        confirmed=True,
        link=item,
        result=result,
        profile="curl_chrome_h2",
        confirmation=dict(
            url=url,
            status=200,
            events=[dict(url=url)],
            audit=dict(texto_util_confirmado=True, artigo_acessivel=True, audio_dedicado=False),
        ),
    )


class Qualified:
    def __init__(self, db, target_schema):
        self.db = db
        self.schema = target_schema

    def execute(self, query, params=None):
        return self.db.execute(query.replace("midias_manager.", self.schema + "."), params)


def test_automatic_restore_persists_profile_and_original_history(ingest, applier):
    with connect_postgres() as db:
        db.row_factory = dict_row
        db.execute(f"SET LOCAL search_path TO {ingest.store.schema},pg_catalog")
        db.execute("UPDATE links SET active=0,deleted_at=now(),version=version+1 WHERE id=1")
        db.execute(
            "INSERT INTO events(entity,entity_id,action,happened_at,after_json) "
            "VALUES('endpoint',1,'validation_exclude',now(),%s)",
            (Jsonb(dict(version=2)),),
        )
        row = fixture_row(ingest, db)
        models = {
            r["model"]: r["version"] for r in db.execute("SELECT model,version FROM mechanisms")
        }
        result = applier.apply_row(
            Qualified(db, ingest.store.schema), row, dict(id="test-run"), {}, models
        )
        assert result["action"] == "restored"
    endpoint = ingest.store.endpoint(1)
    assert endpoint["active"] and endpoint["access_transport"] == "curl_chrome_h2"
    assert endpoint["last_test"]["independent_confirmation"]["audit"]["texto_util_confirmado"]
    assert endpoint["test_config_current"]
    with ingest.connection() as db:
        history = db.execute("SELECT result FROM link_test_results WHERE link_id=1").fetchall()
        assert any(row["result"]["test_id"] == "proof-1" for row in history)
        events = db.execute(
            "SELECT action FROM events WHERE entity='endpoint' AND entity_id=1"
        ).fetchall()
        assert any(row["action"] == "validation_exclude" for row in events)
        assert any(row["action"] == "transport_recovery" for row in events)


def test_manual_deletion_and_source_edits_are_preserved(ingest, applier):
    with connect_postgres() as db:
        db.row_factory = dict_row
        db.execute(f"SET LOCAL search_path TO {ingest.store.schema},pg_catalog")
        db.execute("UPDATE links SET active=0,deleted_at=now(),version=version+1 WHERE id=1")
        db.execute(
            "INSERT INTO events(entity,entity_id,action,happened_at) "
            "VALUES('endpoint',1,'delete',now())"
        )
        row = fixture_row(ingest, db)
        models = {
            r["model"]: r["version"] for r in db.execute("SELECT model,version FROM mechanisms")
        }
        wrapped = Qualified(db, ingest.store.schema)
        result = applier.apply_row(wrapped, row, dict(id="test-run"), {}, models)
        assert result["action"] == "manual_deletion_preserved"
        db.execute("UPDATE sites SET version=version+1 WHERE id=1")
        assert (
            applier.apply_row(wrapped, row, dict(id="test-run"), {}, models)["action"]
            == "source_changed"
        )


def test_snapshot_and_reader_keep_the_configured_profile(ingest, monkeypatch):
    endpoint = ingest.store.endpoint(1)
    ingest.store.save(
        "endpoint", dict(version=endpoint["version"], access_transport="curl_chrome_h1"), 1
    )
    run = ingest.create_run({})
    with ingest.connection() as db:
        row = db.execute(
            "SELECT snapshot FROM ingest_polls WHERE run_id=%s", (run["id"],)
        ).fetchone()
    snapshot = row["snapshot"]
    assert snapshot["access_transport"] == "curl_chrome_h1"
    captured = {}

    class Fake:
        def close(self):
            captured["closed"] = True

    def factory(roots, progress, **kwargs):
        captured.update(kwargs)
        return Fake()

    monkeypatch.setattr("buscador_midias.ingestion.engine.make_reader", factory)
    with Engine(ingest).reader(dict(snapshot=snapshot), ["https://jose.example"]):
        pass
    assert captured["transport"] == "curl_chrome_h1" and captured["closed"]
