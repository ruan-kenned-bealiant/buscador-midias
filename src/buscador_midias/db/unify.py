"""Migração dos sites do ``bm_sites`` para ``midias_ia_trusted_sources``.

A lista de sites do buscador e a das fontes confiáveis do corpus eram a mesma lista mantida em dois
lugares. Esta migração faz do buscador o dono da tabela do corpus: cada site adota a linha de
mesmo endereço (a de menor id, quando a lista original repete o endereço) ou, se não houver, vira
uma linha nova. Os ids das tabelas ``bm_*`` são refeitos e ``bm_sites`` deixa de existir.

É idempotente: sem ``bm_sites`` não há o que migrar.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import tuple_row

from buscador_midias.db.schema import INSTALL_LOCK, install

SITES = "midias_ia_trusted_sources"
_DELETE_ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL"}
# Só o que o buscador sabe sobre o site passa para a linha adotada; o cadastro curado do corpus
# (nome, país, idioma, tipo) vale, e os campos vazios são completados pelo que o buscador tinha.
_ADOPT = f"""
UPDATE {SITES} t SET identity=s.identity,deleted_at=s.deleted_at,version=s.version,
 origin=s.origin,deactivated_by=s.deactivated_by,deactivated_reason=s.deactivated_reason,
 deactivated_at=s.deactivated_at,active=(s.active=1),
 notes=CASE WHEN t.notes='' THEN s.notes ELSE t.notes END,
 country=COALESCE(NULLIF(t.country,''),s.country),
 language=COALESCE(NULLIF(t.language,''),s.language)
FROM bm_sites s JOIN site_map m ON m.old_id=s.id WHERE t.id=m.new_id
"""
_CREATE = f"""
WITH created AS (
 INSERT INTO {SITES}(name,url,url_without_https_www,country,language,notes,active,identity,
  deleted_at,version,origin,deactivated_by,deactivated_reason,deactivated_at,import_source,
  created_at,updated_at)
 SELECT s.name,s.url,s.identity,s.country,s.language,s.notes,(s.active=1),s.identity,s.deleted_at,
  s.version,s.origin,s.deactivated_by,s.deactivated_reason,s.deactivated_at,'buscador',
  s.created_at,s.updated_at
 FROM bm_sites s WHERE s.id NOT IN (SELECT old_id FROM site_map) ORDER BY s.id
 RETURNING id,identity)
INSERT INTO site_map(old_id,new_id) SELECT s.id,c.id FROM created c JOIN bm_sites s
 ON s.identity=c.identity
"""


def _scalar(cur: psycopg.Cursor[tuple[Any, ...]], query: str) -> Any:
    row = cur.execute(query).fetchone()
    assert row is not None
    return row[0]


def unify_sites(db: psycopg.Connection[Any]) -> dict[str, Any]:
    """Move os sites para a tabela do corpus; devolve o resumo (``migrated=False`` se já feito)."""
    cur = db.cursor(row_factory=tuple_row)
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (INSTALL_LOCK,))
    if _scalar(cur, "SELECT to_regclass('bm_sites') IS NULL"):
        return dict(migrated=False)
    install(db)  # acrescenta à tabela do corpus as colunas que o buscador usa
    cur.execute(
        "CREATE TEMP TABLE site_map(old_id bigint PRIMARY KEY, new_id bigint) ON COMMIT DROP"
    )
    cur.execute(
        f"INSERT INTO site_map SELECT s.id,min(t.id) FROM bm_sites s JOIN {SITES} t "
        "ON lower(rtrim(t.url_without_https_www,'/'))=lower(rtrim(s.identity,'/')) "
        "AND t.identity IS NULL GROUP BY s.id"
    )
    matched = _scalar(cur, "SELECT count(*) FROM site_map")
    duplicated = _scalar(
        cur, "SELECT count(*) FROM (SELECT new_id FROM site_map GROUP BY 1 HAVING count(*)>1) x"
    )
    if duplicated:
        raise RuntimeError("Dois sites do buscador adotariam a mesma fonte confiável.")
    cur.execute(_ADOPT)
    cur.execute(_CREATE)
    references = cur.execute(
        "SELECT conrelid::regclass::text AS tbl,a.attname AS col,conname,confdeltype "
        "FROM pg_constraint c JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1] "
        "WHERE c.contype='f' AND c.confrelid='bm_sites'::regclass"
    ).fetchall()
    moved: dict[str, int] = {}
    for table, column, name, _action in references:
        cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
        moved[table] = cur.execute(
            f"UPDATE {table} t SET {column}=m.new_id FROM site_map m WHERE t.{column}=m.old_id"
        ).rowcount
    events = cur.execute(
        "UPDATE bm_events e SET entity_id=m.new_id FROM site_map m "
        "WHERE e.entity='source' AND e.entity_id=m.old_id"
    ).rowcount
    for table, column, name, action in references:
        cur.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({column}) "
            f"REFERENCES {SITES}(id) ON DELETE {_DELETE_ACTIONS[action]}"
        )
    cur.execute("DROP TABLE bm_sites")
    total = _scalar(cur, "SELECT count(*) FROM site_map")
    return dict(
        migrated=True,
        matched=matched,
        created=total - matched,
        references=moved,
        events=events,
    )
