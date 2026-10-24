"""Migração local preserva todos os dados e impede relacionamentos órfãos."""

import json
import sqlite3

import pytest

from buscador_midias.catalog.schema import LINK_COLUMNS
from buscador_midias.catalog.store import CatalogStore


@pytest.fixture
def catalog(tmp_path):
    sites = tmp_path / "sites.csv"
    sites.write_text(
        "region,name,url,country,country_code,language,source_type\n"
        "América,Site,https://news.example,Brasil,BR,Português,portal\n"
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            dict(
                sources=[dict(key="news.example", name="Site")],
                mechanisms={
                    "news.example": [
                        dict(url="https://news.example/feed/", kind="feed", evidence="xml", texts=1)
                    ]
                },
            )
        )
    )
    store = CatalogStore(tmp_path / "catalog.sqlite", snapshot, sites)
    store.initialize()
    return store


def legacy_database(catalog, *, invalid_model=False):
    """Recria o formato anterior, sem FK para mecanismo, mantendo o conteúdo original."""
    with catalog.connection() as db:
        before = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("sites", "links", "mechanisms", "events", "metadata")
        }
        db.execute("DROP TABLE link_test_results")
        db.execute("DROP TABLE schema_migrations")
        db.execute("ALTER TABLE links RENAME TO links_old")
        db.execute("""CREATE TABLE endpoints (
            id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sites(id),
            url TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,notes TEXT NOT NULL,
            active INTEGER NOT NULL,deleted_at TEXT,version INTEGER NOT NULL,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,evidence TEXT NOT NULL,
            origin TEXT NOT NULL,model TEXT NOT NULL,last_test TEXT NOT NULL,
            UNIQUE(source_id,url))""")
        db.execute(f"INSERT INTO endpoints SELECT {LINK_COLUMNS} FROM links_old")
        db.execute("DROP TABLE links_old")
        db.execute("ALTER TABLE sites RENAME TO sources")
        db.execute("ALTER TABLE mechanisms RENAME TO mechanism_settings")
        if invalid_model:
            db.execute("UPDATE endpoints SET model='does_not_exist'")
    return before


def reopened(catalog):
    return CatalogStore(catalog.path, catalog.snapshot, catalog.sites)


def test_legacy_migration_is_lossless_and_idempotent(catalog):
    before = legacy_database(catalog)
    migrated = reopened(catalog)
    migrated.initialize()
    for store in (migrated, reopened(catalog)):
        with store.connection() as db:
            for table, rows in before.items():
                assert [tuple(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY 1")] == rows
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not list(db.execute("PRAGMA foreign_key_check"))
            assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 3
            assert not list(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('sources','endpoints','mechanism_settings')"
                )
            )


def test_links_require_existing_site_and_mechanism(catalog):
    with catalog.connection() as db:
        keys = {(r[2], r[3], r[4]) for r in db.execute("PRAGMA foreign_key_list(links)")}
        assert ("sites", "source_id", "id") in keys
        assert ("mechanisms", "model", "model") in keys
        for assignment in ("source_id=999999", "model='unknown'", "model=NULL"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE links SET " + assignment)
        for statement in ("DELETE FROM sites", "DELETE FROM mechanisms WHERE model='rss'"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)


def test_failed_migration_rolls_back_names_and_data(catalog):
    legacy_database(catalog, invalid_model=True)
    with pytest.raises(sqlite3.IntegrityError):
        reopened(catalog).initialize()
    with sqlite3.connect(catalog.path) as db:
        assert db.execute("SELECT model FROM endpoints").fetchone()[0] == "does_not_exist"
        assert db.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='sites'").fetchone()


def test_saved_test_history_keeps_snapshots_after_config_changes_and_deletion(catalog):
    source = catalog.sources()["items"][0]
    endpoint = catalog.endpoints(source["id"])["items"][0]
    result = dict(
        test_id="relational-test",
        source_id=source["id"],
        source_url=source["url"],
        url=endpoint["url"],
        model="rss",
        model_version=1,
        parameters={"article_samples": 1},
        requested_kind="feed",
        xml_valid=True,
        compatible=True,
        root_entries=1,
        root_children=0,
        root_index=False,
        observed_at="2026-10-23T12:00:00Z",
        text_samples=1,
        samples=[dict(url="https://news.example/article", text=True)],
        outcome="text_confirmed",
    )
    catalog.record_test(endpoint["id"], endpoint["version"], result)
    catalog.save_model("rss", dict(version=1, parameters={"article_samples": 2}))
    catalog.delete("source", source["id"], source["version"])
    with catalog.connection() as db:
        row = db.execute("SELECT * FROM link_test_results").fetchone()
        assert row["link_id"] == endpoint["id"] and row["model"] == "rss"
        assert json.loads(row["result"]) == result
        assert json.loads(row["parameters"]) == {"article_samples": 1}
        assert not list(db.execute("PRAGMA foreign_key_check"))


def test_legacy_audit_tests_are_backfilled_without_reassigning_copied_proofs(catalog):
    link = catalog.endpoints(catalog.sources()["items"][0]["id"])["items"][0]
    legacy_database(catalog)
    older = dict(test_id="old", model="rss", parameters={"article_samples": 1})
    latest = dict(test_id="latest", model="rss", parameters={"article_samples": 2})
    copied = dict(test_id="from-another-link", model="rss", parameters={})
    with sqlite3.connect(catalog.path) as db:
        db.execute("UPDATE endpoints SET last_test=? WHERE id=?", (json.dumps(latest), link["id"]))
        for ident, result in [(link["id"], older), (999, copied)]:
            db.execute(
                "INSERT INTO events(entity,entity_id,action,happened_at,before_json) "
                "VALUES('endpoint',?,'update','2026-10-23',?)",
                (link["id"], json.dumps(dict(id=ident, last_test=json.dumps(result)))),
            )
    with reopened(catalog).connection() as db:
        results = {
            r[0]: json.loads(r[1])
            for r in db.execute("SELECT test_id,result FROM link_test_results")
        }
    assert results == {"old": older, "latest": latest}


def test_search_interval_defaults_edit_preserves_proof_and_restarts(catalog):
    from buscador_midias.catalog.store import CatalogError

    source = catalog.sources()["items"][0]
    link = catalog.endpoints(source["id"])["items"][0]
    assert link["search_interval_seconds"] == 14400
    for value in (0, -1, True, "3600", 1.5, None, 2**63):
        with pytest.raises(CatalogError):
            catalog.save(
                "endpoint", dict(version=link["version"], search_interval_seconds=value), link["id"]
            )
    saved = catalog.save(
        "endpoint", dict(version=link["version"], search_interval_seconds=7200), link["id"]
    )
    with pytest.raises(CatalogError):
        catalog.save(
            "endpoint", dict(version=link["version"], search_interval_seconds=3600), link["id"]
        )
    restored = reopened(catalog).endpoints(source["id"])["items"][0]
    assert restored["search_interval_seconds"] == 7200
    assert restored["evidence"] == link["evidence"]
    assert restored["last_test"] == link["last_test"]
    assert restored["version"] == saved["version"]
    new = catalog.save(
        "endpoint",
        dict(url="https://news.example/other.xml", kind="sitemap"),
        source_id=source["id"],
    )
    assert new["search_interval_seconds"] == 14400


def test_search_interval_migration_uses_cadence_once_and_retains_history(catalog):
    source = catalog.sources()["items"][0]
    first = catalog.endpoints(source["id"])["items"][0]
    second = catalog.save(
        "endpoint",
        dict(url="https://news.example/other.xml", kind="sitemap"),
        source_id=source["id"],
    )
    with catalog.connection() as db:
        db.execute("ALTER TABLE links DROP COLUMN search_interval_seconds")
        db.execute("DELETE FROM schema_migrations WHERE version=3")
        db.execute(
            "CREATE TABLE link_cadence (link_id INTEGER PRIMARY KEY, "
            "status TEXT, interval_seconds REAL)"
        )
        db.execute("INSERT INTO link_cadence VALUES(?,'estimated',3600.4)", (first["id"],))
        db.execute("INSERT INTO link_cadence VALUES(?,'unknown',NULL)", (second["id"],))
        original = db.execute(
            "SELECT evidence,last_test,version FROM links WHERE id=?", (first["id"],)
        ).fetchone()
    store = reopened(catalog)
    links = {link["id"]: link for link in store.endpoints(source["id"])["items"]}
    assert links[first["id"]]["search_interval_seconds"] == 3600
    assert links[second["id"]]["search_interval_seconds"] == 14400
    with store.connection() as db:
        assert tuple(
            db.execute(
                "SELECT evidence,last_test,version FROM links WHERE id=?", (first["id"],)
            ).fetchone()
        ) == tuple(original)
        db.execute("UPDATE link_cadence SET interval_seconds=60")
    assert reopened(catalog).endpoint(first["id"])["search_interval_seconds"] == 3600
