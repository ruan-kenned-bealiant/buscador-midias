"""DDL do cadastro novo: tabelas ``bm_*`` com as restrições que sustentam o desenho."""

from __future__ import annotations

import os

import psycopg
import pytest

from buscador_midias.db.schema import CATALOG_TABLES, install

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def _tables(db: psycopg.Connection) -> set[str]:
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()"
    ).fetchall()
    return {row[0] for row in rows}


def _site(db: psycopg.Connection, identity: str = "g1.globo.com") -> int:
    row = db.execute(
        "INSERT INTO bm_sites(identity,name,url) VALUES(%s,'G1','https://g1.globo.com') "
        "RETURNING id",
        (identity,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _mechanism(db: psycopg.Connection, model: str = "rss") -> str:
    db.execute(
        "INSERT INTO bm_mechanisms(model,parameters) VALUES(%s,'{}') ON CONFLICT DO NOTHING",
        (model,),
    )
    return model


def _link(db: psycopg.Connection, site: int, url: str = "https://g1.globo.com/rss", **extra: str):
    columns = {"site_id": site, "url": url, "kind": "feed", "mechanism": _mechanism(db), **extra}
    names = ",".join(columns)
    marks = ",".join(["%s"] * len(columns))
    row = db.execute(
        f"INSERT INTO bm_links({names}) VALUES({marks}) RETURNING id", tuple(columns.values())
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_install_creates_only_prefixed_catalog_tables(db):
    install(db)
    tables = _tables(db)
    assert set(CATALOG_TABLES) <= tables
    assert all(name.startswith("bm_") for name in tables)
    assert {"bm_sites", "bm_links", "bm_link_state", "bm_mechanisms", "bm_settings"} <= tables


def test_install_is_idempotent_and_keeps_data(db):
    install(db)
    site = _site(db)
    install(db)
    assert db.execute("SELECT count(*) FROM bm_sites WHERE id=%s", (site,)).fetchone()[0] == 1


def test_link_is_unique_per_site_and_url_even_when_deleted(db):
    install(db)
    site = _site(db)
    link = _link(db, site)
    db.execute("UPDATE bm_links SET deleted_at=now() WHERE id=%s", (link,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        _link(db, site)


def test_link_requires_existing_site_and_mechanism(db):
    install(db)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _link(db, 999999)


@pytest.mark.parametrize(
    ("column", "value"),
    [("kind", "html"), ("read_order", "random"), ("route", "daily"), ("deactivated_by", "robot")],
)
def test_link_rejects_values_outside_the_contract(db, column, value):
    install(db)
    site = _site(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        _link(db, site, **{column: value})


def test_link_accepts_the_documented_values(db):
    install(db)
    site = _site(db)
    link = _link(db, site, read_order="newest_first", route="updates", deactivated_by="health")
    row = db.execute(
        "SELECT access_transport,search_interval_seconds,route FROM bm_links WHERE id=%s", (link,)
    ).fetchone()
    assert row == ("direct", 14400, "updates")


def test_link_state_is_one_to_one_with_its_link(db):
    install(db)
    link = _link(db, _site(db))
    db.execute("INSERT INTO bm_link_state(link_id) VALUES(%s)", (link,))
    state = db.execute(
        "SELECT health_failures,last_test FROM bm_link_state WHERE link_id=%s", (link,)
    ).fetchone()
    assert state == (0, {})
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO bm_link_state(link_id) VALUES(%s)", (link,))


def test_machine_state_changes_do_not_touch_the_link_version(db):
    install(db)
    link = _link(db, _site(db))
    db.execute("INSERT INTO bm_link_state(link_id) VALUES(%s)", (link,))
    before = db.execute("SELECT version,updated_at FROM bm_links WHERE id=%s", (link,)).fetchone()
    db.execute(
        "UPDATE bm_link_state SET health_failures=2,last_test='{\"ok\":true}' WHERE link_id=%s",
        (link,),
    )
    assert (
        db.execute("SELECT version,updated_at FROM bm_links WHERE id=%s", (link,)).fetchone()
        == before
    )


def test_settings_hold_one_row_per_scope(db):
    install(db)
    db.execute("INSERT INTO bm_settings(scope,value) VALUES('search','{}')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO bm_settings(scope,value) VALUES('search','{}')")


def test_events_record_a_diff_not_the_whole_row(db):
    install(db)
    db.execute(
        "INSERT INTO bm_events(entity,entity_id,action,actor,changes) "
        "VALUES('link',1,'deactivate','health','{\"active\":[1,0]}')"
    )
    row = db.execute("SELECT actor,changes,happened_at FROM bm_events").fetchone()
    assert row[0] == "health"
    assert row[1] == {"active": [1, 0]}
    assert row[2] is not None


def test_fold_function_ignores_accents_and_case(db):
    install(db)
    value = db.execute("SELECT bm_fold('São José – Ñandú ÇÀ')").fetchone()[0]
    assert value == "sao jose – nandu ca"
    assert db.execute("SELECT strpos(bm_fold('Folha de S.Paulo'),bm_fold('PAULO'))>0").fetchone()[0]
