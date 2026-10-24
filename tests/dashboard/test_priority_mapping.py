"""Gravação real da rodada dirigida em esquema PostgreSQL descartável."""

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from test_catalog_health_postgres import auto_exclude, health, old_news  # noqa: F401
from test_catalog_postgres import local, schema  # noqa: F401

from buscador_midias.catalog.store import CatalogError
from buscador_midias.catalog.testing import run_test

spec = importlib.util.spec_from_file_location(
    "priority_catalog_apply",
    Path(__file__).resolve().parents[2] / "scripts/priority_catalog_apply.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
apply_source = module.apply_source

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL explícito"
)


def proof(h, url):
    """Confere prova, auditoria e preservação dos cadastros existentes."""
    s = h.store.source(1)
    m = h.store.model_settings("rss")
    result = run_test(
        dict(
            test_id=uuid.uuid4().hex,
            url=url,
            kind="feed",
            model="rss",
            model_version=m["version"],
            parameters=m["parameters"],
        ),
        s,
        lambda _: None,
        old_news,
    )
    assert result["usable_for_news"]
    return dict(source=s, results=[dict(result=result, provenance="teste local")])


def test_create_proof_dedup_and_default_interval(health):  # noqa: F811
    """Confere prova, auditoria e preservação dos cadastros existentes."""
    d = proof(health, "https://jose.example/newfeed")
    rows = apply_source(health, d)
    assert rows[0]["action"] == "priority_mapping_create"
    link = health.store.endpoint(rows[0]["link_id"])
    assert link["active"] and link["search_interval_seconds"] == 14400
    assert link["last_test"]["usable_for_news"]
    assert apply_source(health, d)[0]["action"] == "already_present"


def test_restore_auto_and_preserve_manual(health):  # noqa: F811
    """Confere prova, auditoria e preservação dos cadastros existentes."""
    auto_exclude(health)
    d = proof(health, "https://jose.example/feed/")
    assert apply_source(health, d)[0]["action"] == "priority_mapping_restore"
    assert health.store.endpoint(1)["active"]
    health.store.delete("endpoint", 1, health.store.endpoint(1)["version"])
    d = proof(health, "https://jose.example/feed/")
    assert apply_source(health, d)[0]["action"] == "preserved_manual"
    with pytest.raises(CatalogError):
        health.store.endpoint(1)


def test_no_positive_no_changes_and_source_edit_protection(health):  # noqa: F811
    """Confere prova, auditoria e preservação dos cadastros existentes."""
    d = proof(health, "https://jose.example/newfeed")
    health.store.save("source", dict(version=d["source"]["version"], notes="edição manual"), 1)
    assert apply_source(health, d)[0]["action"] == "skipped"
    with health.connection() as db:
        assert db.execute("SELECT count(*) n FROM links").fetchone()["n"] == 1


def test_bbc_official_domain_requires_text_and_preserves_source_identity(health):  # noqa: F811
    """Confere prova, auditoria e preservação dos cadastros existentes."""
    with health.connection() as db:
        before = db.execute("SELECT * FROM sites WHERE id=1").fetchone()
        after = db.execute(
            "UPDATE sites SET "
            "url='https://www.bbc.com/news',identity='bbc.com/news',active=0,version=version+1"
            " WHERE id=1 RETURNING *"
        ).fetchone()
        db.execute(
            "INSERT INTO "
            "events(entity,entity_id,action,happened_at,before_json,after_json) "
            "VALUES('source',1,'validation_inactivate',now(),%s,%s)",
            (Jsonb(health.jsonable(before)), Jsonb(health.jsonable(after))),
        )
    source = health.store.source(1)
    proposed = "https://www.bbc.co.uk/news"
    model = health.store.model_settings("rss")
    result = run_test(
        dict(
            test_id=uuid.uuid4().hex,
            url="https://feeds.bbci.co.uk/news/rss.xml",
            kind="feed",
            model="rss",
            model_version=model["version"],
            parameters=model["parameters"],
        ),
        dict(source, url=proposed),
        lambda _: None,
        old_news,
    )
    assert result["usable_for_news"]
    data = dict(
        source=source,
        proposed_source_url=proposed,
        results=[dict(result=result, provenance="RSS oficial BBC")],
    )
    decisions = apply_source(health, data)
    assert [r["action"] for r in decisions] == [
        "source_official_domain",
        "priority_mapping_create",
        "source_activated",
    ]
    actual = health.store.source(1)
    assert actual["url"] == proposed and actual["active"]
    with health.connection() as db:
        assert db.execute("SELECT count(*) n FROM sites").fetchone()["n"] == 1
        assert (
            db.execute(
                "SELECT count(*) n FROM events WHERE action='validation_inactivate'"
            ).fetchone()["n"]
            == 1
        )
