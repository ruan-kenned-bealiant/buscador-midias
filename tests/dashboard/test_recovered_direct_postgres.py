"""Aplicação transacional da recuperação em schema PostgreSQL isolado."""

import os

import pytest
from psycopg.types.json import Jsonb
from scripts.validate_recovered_direct import apply
from test_catalog_health_postgres import old_news
from test_catalog_postgres import local, schema  # noqa: F401

from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.migrate_postgres import migrate_catalog
from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.catalog.testing import run_test
from buscador_midias.ingestion.store import IngestStore

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1",
    reason="Requer PostgreSQL explícito em schema isolado",
)


@pytest.mark.parametrize("mode", ["positive", "negative", "concurrent", "manual"])
def test_restore_preserves_history_versions_and_user_edits(local, schema, mode):  # noqa: F811
    migrate_catalog(local.path, schema)
    repository = IngestStore(PostgresCatalogStore(schema))
    repository.ready = True
    with repository.connection() as db:
        db.execute("UPDATE links SET active=0,deleted_at=now(),version=version+1 WHERE id=1")
        db.execute("UPDATE sites SET active=0 WHERE id=1")
        source = db.execute("SELECT * FROM sites WHERE id=1").fetchone()
        link = db.execute("SELECT * FROM links WHERE id=1").fetchone()
        model = db.execute("SELECT * FROM mechanisms WHERE model='rss'").fetchone()
        db.execute(
            "INSERT INTO events(entity,entity_id,action,happened_at,after_json) "
            "VALUES('source',1,'validation_inactivate',now(),%s)",
            (Jsonb({"version": source["version"]}),),
        )
    original = dict(HealthStore.jsonable(link), source_version=source["version"])
    config = dict(
        url=link["url"],
        kind="feed",
        model="rss",
        model_version=model["version"],
        parameters={"lookback_days": 0},
        test_id="test-restore",
    )
    source = dict(id=source["id"], name=source["name"], url=source["url"])
    result = run_test(config, source, lambda _: None, old_news)
    assert result["usable_for_news"]
    if mode == "negative":
        result.update(usable_for_news=False, text_samples=0)
    if mode == "concurrent":
        with repository.connection() as db:
            db.execute("UPDATE links SET version=version+1 WHERE id=1")
    snapshot = dict(original=original, config=config, automatic_exclusion=mode != "manual")
    action = apply(repository, {"id": "run"}, snapshot, result)
    expected = dict(
        positive="restored",
        negative="not_confirmed",
        concurrent="concurrent_edit",
        manual="manual_deletion_preserved",
    )[mode]
    assert action == expected
    assert apply(repository, {"id": "run"}, snapshot, result) == "already_recorded"
    with repository.connection() as db:
        link = db.execute("SELECT * FROM links WHERE id=1").fetchone()
        source = db.execute("SELECT * FROM sites WHERE id=1").fetchone()
        assert bool(link["active"]) is (mode == "positive")
        assert bool(source["active"]) is (mode == "positive")
        assert (link["deleted_at"] is None) is (mode == "positive")
        assert link["search_interval_seconds"] == original["search_interval_seconds"]
        assert (
            db.execute(
                "SELECT COUNT(*) n FROM link_test_results WHERE test_id=%s", ("test-restore",)
            ).fetchone()["n"]
            == 1
        )
        events = db.execute("SELECT * FROM events WHERE action='direct_text_restore'").fetchall()
        assert len(events) == (1 if mode == "positive" else 0)
        if events:
            assert events[0]["before_json"]["deleted_at"]
            assert events[0]["after_json"]["validation_run_id"] == "run"
