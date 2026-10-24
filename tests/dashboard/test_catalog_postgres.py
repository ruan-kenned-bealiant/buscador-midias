"""Migração e CRUD reais em esquema descartável da base PostgreSQL de teste."""

import json
import os
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg import sql

from buscador_midias.catalog.batch import initialize_batch, status
from buscador_midias.catalog.migrate_postgres import migrate_catalog
from buscador_midias.catalog.postgres import PostgresCatalogStore, connect_postgres
from buscador_midias.catalog.store import CatalogError, CatalogStore

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.fixture
def local(tmp_path):
    csv = tmp_path / "sites.csv"
    csv.write_text(
        "region,name,url,country,country_code,language,source_type\n"
        "América,São José,https://jose.example,Brasil,BR,Português,portal\n"
    )
    snapshot = tmp_path / "retrato.json"
    snapshot.write_text(
        json.dumps(
            dict(
                sources=[dict(key="jose.example", name="São José")],
                mechanisms={
                    "jose.example": [dict(url="https://jose.example/feed/", kind="feed", texts=1)]
                },
            )
        )
    )
    store = CatalogStore(tmp_path / "catalogo.sqlite", snapshot, csv)
    store.initialize()
    initialize_batch(store)
    with store.connection() as db:
        db.execute(
            "INSERT INTO catalog_validation_runs VALUES('run','completed',?,?,?,?,?,?,?)",
            (
                "2026-10-23T00:00:00Z",
                "2026-10-23T00:00:00Z",
                "2026-10-23T00:00:00Z",
                365,
                3,
                "{}",
                "",
            ),
        )
        db.execute(
            "INSERT INTO catalog_validation_links "
            "VALUES('run',1,1,1,'jose.example','rss','kept',1,0,?)",
            ("2026-10-23T00:00:00Z",),
        )
        db.execute(
            "INSERT INTO catalog_validation_attempts "
            "VALUES('run',1,1,'rss','text_confirmed',1,?,?)",
            (zlib.compress(b'{"text":true}'), "2026-10-23T00:00:00Z"),
        )
    return store


@pytest.fixture
def schema():
    name = "bm_test_catalog_" + uuid.uuid4().hex
    yield name
    with connect_postgres() as db:
        db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def test_migration_preserves_all_rows_and_is_idempotent(local, schema):
    report = migrate_catalog(local.path, schema)
    assert report["action"] == "imported"
    assert migrate_catalog(local.path, schema, check_only=True)["tables"] == report["tables"]
    pg = PostgresCatalogStore(schema)
    assert pg.summary() == local.summary()
    assert pg.sources(q="sao jose")["total"] == 1
    assert pg.endpoints(1, q="FEED")["total"] == 1
    assert pg.endpoint(1)["search_interval_seconds"] == 14400
    assert len(pg.models()) == 6
    assert status(pg)["attempts"] == 1
    with pg.connection() as db:
        assert (
            zlib.decompress(
                db.execute("SELECT result FROM catalog_validation_attempts").fetchone()[0]
            )
            == b'{"text":true}'
        )
    result = dict(
        test_id="pg-proof",
        source_id=1,
        source_url="https://jose.example",
        url="https://jose.example/feed/",
        model="rss",
        model_version=1,
        parameters={"article_samples": 1},
        requested_kind="feed",
        xml_valid=True,
        compatible=True,
        root_entries=1,
        root_children=0,
        root_index=False,
        observed_at="2026-10-23T00:00:00Z",
        text_samples=1,
        samples=[dict(url="https://jose.example/noticia", text=True)],
        outcome="text_confirmed",
        assessment_version=1,
        usable_for_news=True,
    )
    pg.record_test(1, 1, result)
    assert pg.endpoint(1)["test_config_current"]
    with pg.connection() as db:
        assert (
            json.loads(
                db.execute("SELECT result FROM link_test_results WHERE link_id=1").fetchone()[0]
            )
            == result
        )
    site = pg.save("source", dict(name="Nova", url="https://nova.example"))
    assert site["id"] > 1
    link = pg.save(
        "endpoint", dict(url="https://nova.example/feed/", kind="feed"), source_id=site["id"]
    )
    assert link["id"] > 1
    with pytest.raises(CatalogError) as duplicate:
        pg.save(
            "endpoint", dict(url="https://nova.example/feed/", kind="feed"), source_id=site["id"]
        )
    assert duplicate.value.status == 409
    changed = pg.save("endpoint", dict(version=1, search_interval_seconds=7200), link["id"])
    assert changed["search_interval_seconds"] == 7200
    pg.save_model("rss", dict(version=1, parameters={"article_samples": 2}))
    assert pg.model_settings("rss")["version"] == 2
    pg.delete("source", site["id"], site["version"])
    assert pg.summary()["sources"]["total"] == 1
    with pytest.raises(ValueError, match="divergente"):
        migrate_catalog(local.path, schema, check_only=True)


def test_concurrent_edits_do_not_overwrite(local, schema):
    migrate_catalog(local.path, schema)
    pg = PostgresCatalogStore(schema)

    def update(interval):
        try:
            return pg.save("endpoint", dict(version=1, search_interval_seconds=interval), 1)[
                "search_interval_seconds"
            ]
        except CatalogError as error:
            return error.status

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(update, [3600, 7200]))
    assert results.count(409) == 1
    assert pg.endpoint(1)["version"] == 2
    assert pg.endpoint(1)["evidence"] == local.endpoint(1)["evidence"]


def test_failed_copy_rolls_back_entire_schema(local, schema):
    with local.connection() as db:
        db.execute("UPDATE links SET evidence='invalid json'")
    with pytest.raises(ValueError):
        migrate_catalog(local.path, schema)
    with connect_postgres() as db:
        assert not db.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()


def test_cli_activation_selects_only_postgres_and_keeps_sqlite_override(local, schema, tmp_path):
    from buscador_midias.catalog.storage import open_catalog
    from buscador_midias.cli import main

    report = tmp_path / "migration.json"
    assert (
        main(
            [
                "migrate-catalog",
                str(local.path),
                "--schema",
                schema,
                "--activate",
                "--report",
                str(report),
            ]
        )
        == 0
    )
    assert json.loads(report.read_text())["action"] == "imported"
    store = open_catalog(local.path, local.snapshot, local.sites)
    assert isinstance(store, PostgresCatalogStore)
    assert store.schema == schema
    assert store.summary() == local.summary()
    assert (
        type(open_catalog(local.path, local.snapshot, local.sites, explicit_sqlite=True))
        is CatalogStore
    )
