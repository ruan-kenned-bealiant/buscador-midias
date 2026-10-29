"""Importação do cadastro legado (``midias_manager``) para as tabelas ``bm_*``."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from buscador_midias.catalog.postgres_schema import DDL as LEGACY_CATALOG_DDL
from buscador_midias.db.connection import connect
from buscador_midias.db.importer import import_legacy, verify
from buscador_midias.db.schema import install
from buscador_midias.db.settings import SEARCH_DEFAULTS

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

LEGACY_EXTRA = """
CREATE TABLE ingest_daily_routes (
 link_id BIGINT PRIMARY KEY REFERENCES links(id), purpose TEXT NOT NULL,
 reason TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE ingest_settings (
 id INT PRIMARY KEY, version INT NOT NULL, config JSONB NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE health_settings (
 kind TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL, interval_minutes INTEGER NOT NULL,
 workers INTEGER NOT NULL, max_attempts INTEGER NOT NULL, version INTEGER NOT NULL,
 next_run_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 bright_data BOOLEAN NOT NULL, max_bright_requests INTEGER NOT NULL,
 activate_recovered BOOLEAN NOT NULL, schedule_hours INTEGER[] NOT NULL,
 mode TEXT NOT NULL, scope TEXT NOT NULL
);
CREATE TABLE link_health (
 link_id BIGINT PRIMARY KEY REFERENCES links(id), run_id TEXT NOT NULL,
 category TEXT NOT NULL, checked_at TIMESTAMPTZ NOT NULL, link_version INTEGER NOT NULL,
 source_version INTEGER NOT NULL, model_version INTEGER NOT NULL,
 failures INTEGER NOT NULL DEFAULT 0
);
"""

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 27, 9, 30, tzinfo=UTC)


@pytest.fixture
def legacy() -> Iterator[str]:
    """Schema legado mínimo, com as tabelas que o importador lê, populado com casos de borda."""
    name = "bm_test_legacy_" + uuid.uuid4().hex[:12]
    with connect(name, create=True) as db:
        db.execute(LEGACY_CATALOG_DDL)
        db.execute(LEGACY_EXTRA)
        for model in ("rss", "sitemap"):
            db.execute(
                "INSERT INTO mechanisms VALUES(%s,'{\"max_requests\":48}',2,%s)", (model, T0)
            )
        db.execute(
            "INSERT INTO sites(id,identity,name,url,country,language,active,version,created_at,"
            "updated_at,origin) VALUES (10,'g1.globo.com','G1','https://g1.globo.com','Brasil',"
            "'Português',1,3,%s,%s,'mapping'),(11,'velho.example','Velho','https://velho.example',"
            "'','',0,1,%s,%s,'manual')",
            (T0, T0, T0, T0),
        )
        db.execute("UPDATE sites SET deleted_at=%s WHERE id=11", (T1,))
        db.execute(
            "INSERT INTO links(id,source_id,url,kind,label,active,version,created_at,updated_at,"
            "origin,model,search_interval_seconds,access_transport,read_order) VALUES "
            "(100,10,'https://g1.globo.com/rss','feed','',1,4,%s,%s,'mapping','rss',7200,"
            "'curl_chrome_h2','newest_first'),"
            "(101,10,'https://g1.globo.com/sitemap.xml','sitemap','',0,2,%s,%s,'mapping',"
            "'sitemap',14400,'direct','unknown'),"
            "(102,11,'https://velho.example/feed','feed','',0,1,%s,%s,'manual','rss',14400,"
            "'direct','unknown')",
            (T0, T0, T0, T0, T0, T0),
        )
        db.execute("UPDATE links SET deleted_at=%s WHERE id=102", (T1,))
        db.execute(
            "INSERT INTO ingest_daily_routes(link_id,purpose,reason) VALUES "
            "(100,'updates','feed diário'),(101,'archive','sitemap histórico')"
        )
        db.execute(
            "INSERT INTO events(entity,entity_id,action,happened_at) VALUES "
            "('endpoint',101,'health_inactivate',%s),('endpoint',102,'validation_exclude',%s),"
            "('endpoint',100,'update',%s),('source',11,'validation_inactivate',%s),"
            "('source',10,'update',%s)",
            (T1, T0, T1, T0, T1),
        )
        db.execute("INSERT INTO link_health VALUES(100,'run-1','healthy',%s,4,3,2,1)", (T1,))
        db.execute(
            "UPDATE links SET last_test=%s,evidence=%s WHERE id=100",
            (
                Jsonb({"model_version": 2, "samples": [{"url": "https://g1.globo.com/a"}]}),
                Jsonb({"texts": 2, "evidence": "xml"}),
            ),
        )
        db.execute("UPDATE links SET evidence=%s WHERE id=102", (Jsonb({"texts": 1}),))
        config = {
            **SEARCH_DEFAULTS,
            "schedule_hours": [1, 6, 12, 16],
            "workers": {**SEARCH_DEFAULTS["workers"], "fetch": 18},
        }
        db.execute("INSERT INTO ingest_settings VALUES(1,23,%s,%s)", (Jsonb(config), T1))
        db.execute(
            "INSERT INTO health_settings(kind,enabled,interval_minutes,workers,max_attempts,"
            "version,next_run_at,bright_data,max_bright_requests,activate_recovered,"
            "schedule_hours,mode,scope) VALUES "
            "('active',true,1440,16,2,5,%s,false,8,false,'{10,20}','probe','errors'),"
            "('recovery',true,1440,8,2,4,NULL,false,8,true,'{10,20}','full','inactive')",
            (T1,),
        )
    yield name
    with connect() as db:
        db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def imported(legacy: str, db: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    install(db)
    import_legacy(db, legacy)
    return db


def test_import_preserves_every_site_link_and_mechanism(imported, legacy):
    counts = verify(imported, legacy)
    assert counts["bm_sites"] == (2, 2)
    assert counts["bm_links"] == (3, 3)
    assert counts["bm_mechanisms"] == (2, 2)
    ids = imported.execute("SELECT id FROM bm_links ORDER BY id").fetchall()
    assert [row[0] for row in ids] == [100, 101, 102]


def test_import_keeps_logically_deleted_rows_so_mappings_do_not_resurrect_them(imported):
    site = imported.execute("SELECT deleted_at FROM bm_sites WHERE id=11").fetchone()
    link = imported.execute("SELECT deleted_at FROM bm_links WHERE id=102").fetchone()
    assert site[0] == T1
    assert link[0] == T1


def test_import_maps_link_attributes_and_the_mechanism_column(imported):
    row = imported.execute(
        "SELECT site_id,mechanism,access_transport,read_order,search_interval_seconds,version "
        "FROM bm_links WHERE id=100"
    ).fetchone()
    assert row == (10, "rss", "curl_chrome_h2", "newest_first", 7200, 4)


def test_daily_routes_become_a_column_of_the_link(imported):
    rows = imported.execute("SELECT id,route FROM bm_links ORDER BY id").fetchall()
    assert rows == [(100, "updates"), (101, "archive"), (102, None)]


def test_deactivation_provenance_comes_from_the_last_relevant_event(imported):
    rows = imported.execute(
        "SELECT id,deactivated_by,deactivated_at FROM bm_links ORDER BY id"
    ).fetchall()
    assert rows[0] == (100, None, None)  # ativo: o evento 'update' não é desativação
    assert rows[1] == (101, "health", T1)
    assert rows[2] == (102, "validation", T0)


def test_site_deactivation_provenance_comes_from_the_last_relevant_event(imported):
    rows = imported.execute(
        "SELECT id,deactivated_by,deactivated_at FROM bm_sites ORDER BY id"
    ).fetchall()
    assert rows[0] == (10, None, None)  # ativo: o evento 'update' não é desativação
    assert rows[1] == (11, "validation", T0)


def test_the_configuration_proved_by_the_health_routine_is_carried_over(imported):
    row = imported.execute("SELECT health_config FROM bm_link_state WHERE link_id=100").fetchone()
    assert row[0] == {
        "url": "https://g1.globo.com/rss",
        "kind": "feed",
        "model": "rss",
        "model_version": 2,
        "source_url": "https://g1.globo.com",
        "access_transport": "curl_chrome_h2",
    }


def test_a_health_check_of_an_outdated_revision_proves_nothing_after_the_import(legacy, db):
    install(db)
    with connect(legacy) as source:
        source.execute("UPDATE links SET version=version+1 WHERE id=100")
    import_legacy(db, legacy)
    row = db.execute(
        "SELECT health_category,health_config FROM bm_link_state WHERE link_id=100"
    ).fetchone()
    assert row == ("healthy", None)


def test_link_health_is_carried_into_the_link_state(imported):
    row = imported.execute(
        "SELECT health_category,health_failures,health_checked_at FROM bm_link_state "
        "WHERE link_id=100"
    ).fetchone()
    assert row == ("healthy", 1, T1)


def test_the_last_test_and_the_evidence_of_each_link_are_carried_over(imported):
    rows = {
        r[0]: r[1:]
        for r in imported.execute("SELECT link_id,last_test,evidence FROM bm_link_state")
    }
    assert rows[100][0]["samples"][0]["url"] == "https://g1.globo.com/a"
    assert rows[100][1] == {"texts": 2, "evidence": "xml"}
    assert rows[102] == ({}, {"texts": 1})


def test_links_with_nothing_to_carry_get_no_state_row(imported):
    ids = [r[0] for r in imported.execute("SELECT link_id FROM bm_link_state ORDER BY 1")]
    assert ids == [100, 102]


def test_settings_are_seeded_per_scope(imported):
    rows = {
        row[0]: row[1:]
        for row in imported.execute(
            "SELECT scope,value,version,next_run_at FROM bm_settings ORDER BY scope"
        )
    }
    assert set(rows) == {"search", "health_active", "health_recovery"}
    assert rows["search"][0]["schedule_hours"] == [1, 6, 12, 16]
    assert rows["search"][1] == 23
    active = rows["health_active"]
    assert active[0]["mode"] == "probe"
    assert active[0]["schedule_hours"] == [10, 20]
    assert active[0]["workers"] == 16
    assert active[2] == T1
    assert rows["health_recovery"][0]["activate_recovered"] is True


def test_new_rows_continue_after_the_imported_ids(imported):
    row = imported.execute(
        "INSERT INTO bm_sites(identity,name,url) VALUES('novo.example','Novo','https://novo') "
        "RETURNING id"
    ).fetchone()
    assert row[0] > 11


def test_import_can_be_repeated_without_duplicating(legacy, db):
    install(db)
    import_legacy(db, legacy)
    import_legacy(db, legacy)
    assert all(a == b for a, b in verify(db, legacy).values())
    assert db.execute("SELECT count(*) FROM bm_links").fetchone()[0] == 3


def test_imported_settings_pass_the_new_validation(imported):
    from buscador_midias.db.settings import validate_health, validate_search_patch

    rows = {r[0]: r[1] for r in imported.execute("SELECT scope,value FROM bm_settings")}
    # A configuração real da busca tem todas as chaves; aqui só as do legado de teste.
    assert validate_search_patch(rows["search"]) == rows["search"]
    for scope in ("health_active", "health_recovery"):
        assert validate_health(scope, rows[scope]) == rows[scope]
