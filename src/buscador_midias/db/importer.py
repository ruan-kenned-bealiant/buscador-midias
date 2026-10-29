"""Importa o cadastro do schema legado (``midias_manager``) para as tabelas ``bm_*``.

Só entram o que o desenho novo preserva: sites, links (inclusive os excluídos logicamente, que
guardam decisões anteriores), mecanismos, a política de rotas diárias, o último estado de saúde
de cada link e as configurações atuais. Rodadas, notícias, eventos e testes ficam para trás.

A importação é repetível: limpa as tabelas de destino do cadastro e recarrega, preservando os ids.
Roda dentro da transação do chamador e confere contagem e checksum antes de devolver.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import psycopg
from psycopg import sql

from buscador_midias.db.schema import INSTALL_LOCK

LEGACY_SCHEMA = "midias_manager"

# Ordem inversa das chaves estrangeiras.
_CLEAR = ("bm_link_state", "bm_links", "bm_sites", "bm_mechanisms", "bm_settings")


class _Check(NamedTuple):
    """Uma tabela conferida após a importação: nomes, expressão de checksum e ordem estável."""

    target: str
    legacy: str
    target_expression: str
    legacy_expression: str
    order: str


_CHECKS = (
    _Check(
        "bm_mechanisms",
        "mechanisms",
        "model || parameters::text || version::text",
        "model || parameters::text || version::text",
        "model",
    ),
    _Check(
        "bm_sites",
        "sites",
        "id::text || identity || url || active::text",
        "id::text || identity || url || active::text",
        "id",
    ),
    _Check(
        "bm_links",
        "links",
        "id::text || site_id::text || url || kind || active::text",
        "id::text || source_id::text || url || kind || active::text",
        "id",
    ),
)

_DEACTIVATION = """
 CASE WHEN l.active = 1 AND l.deleted_at IS NULL THEN NULL ELSE
  CASE last.action
   WHEN 'health_inactivate' THEN 'health' WHEN 'health_delete' THEN 'health'
   WHEN 'validation_exclude' THEN 'validation' WHEN 'delete' THEN 'user' END
 END
"""


class ImportMismatchError(RuntimeError):
    """A contagem ou o checksum do destino não confere com a origem."""


def _q(template: str, source: str) -> sql.Composed:
    return sql.SQL(template).format(src=sql.Identifier(source))


def import_legacy(db: psycopg.Connection[Any], source_schema: str = LEGACY_SCHEMA) -> None:
    """Recarrega o cadastro novo a partir do legado e confere o resultado."""
    db.execute("SELECT pg_advisory_xact_lock(%s)", (INSTALL_LOCK,))
    for table in _CLEAR:
        db.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
    db.execute(
        _q(
            "INSERT INTO bm_mechanisms(model,parameters,version,updated_at) "
            "SELECT model,parameters,version,updated_at FROM {src}.mechanisms",
            source_schema,
        )
    )
    db.execute(_sites_sql(source_schema))
    db.execute(_links_sql(source_schema))
    db.execute(
        _q(
            "INSERT INTO bm_link_state(link_id,last_test,evidence,health_category,"
            "health_failures,health_checked_at,health_config) "
            "SELECT l.id,COALESCE(l.last_test,'{{}}'::jsonb),"
            "COALESCE(l.evidence,'{{}}'::jsonb),h.category,COALESCE(h.failures,0),h.checked_at,"
            # A prova da saúde só vale se o cadastro não mudou desde a verificação.
            "CASE WHEN (h.link_version,h.source_version,h.model_version)="
            "(l.version,s.version,m.version) THEN jsonb_build_object('url',l.url,'kind',l.kind,"
            "'model',l.model,'model_version',m.version,'source_url',s.url,"
            "'access_transport',l.access_transport) END "
            "FROM {src}.links l JOIN {src}.sites s ON s.id=l.source_id "
            "JOIN {src}.mechanisms m ON m.model=l.model "
            "LEFT JOIN {src}.link_health h ON h.link_id=l.id "
            "WHERE h.link_id IS NOT NULL OR l.last_test<>'{{}}'::jsonb "
            "OR l.evidence<>'{{}}'::jsonb",
            source_schema,
        )
    )
    _import_settings(db, source_schema)
    for table in ("bm_sites", "bm_links"):
        db.execute(
            sql.SQL(
                "SELECT setval(pg_get_serial_sequence({name},'id'), "
                "COALESCE((SELECT max(id) FROM {table}),0)+1, false)"
            ).format(name=sql.Literal(table), table=sql.Identifier(table))
        )
    _assert_equal(db, source_schema)


def _sites_sql(source: str) -> sql.Composed:
    return _q(
        "WITH last AS (SELECT DISTINCT ON (entity_id) entity_id,action,happened_at "
        "FROM {src}.events WHERE entity='source' AND action IN "
        "('validation_inactivate','delete') "
        "ORDER BY entity_id,happened_at DESC,id DESC) "
        "INSERT INTO bm_sites(id,identity,name,url,country,language,notes,active,deleted_at,"
        "deactivated_by,deactivated_reason,deactivated_at,version,origin,created_at,updated_at) "
        "OVERRIDING SYSTEM VALUE "
        "SELECT s.id,s.identity,s.name,s.url,s.country,s.language,s.notes,s.active,s.deleted_at,"
        "CASE WHEN s.active = 1 AND s.deleted_at IS NULL THEN NULL ELSE "
        "CASE last.action WHEN 'validation_inactivate' THEN 'validation' WHEN 'delete' THEN 'user' "
        "END END,"
        "CASE WHEN s.active = 1 AND s.deleted_at IS NULL THEN NULL ELSE last.action END,"
        "CASE WHEN s.active = 1 AND s.deleted_at IS NULL THEN NULL ELSE last.happened_at END,"
        "s.version,s.origin,s.created_at,s.updated_at "
        "FROM {src}.sites s LEFT JOIN last ON last.entity_id=s.id",
        source,
    )


def _links_sql(source: str) -> sql.Composed:
    return _q(
        "WITH last AS (SELECT DISTINCT ON (entity_id) entity_id,action,happened_at "
        "FROM {src}.events WHERE entity='endpoint' AND action IN "
        "('health_inactivate','health_delete','validation_exclude','delete') "
        "ORDER BY entity_id,happened_at DESC,id DESC) "
        "INSERT INTO bm_links(id,site_id,url,kind,mechanism,label,notes,active,deleted_at,"
        "deactivated_by,deactivated_reason,deactivated_at,access_transport,read_order,"
        "search_interval_seconds,route,version,origin,created_at,updated_at) "
        "OVERRIDING SYSTEM VALUE "
        "SELECT l.id,l.source_id,l.url,l.kind,l.model,l.label,l.notes,l.active,l.deleted_at,"
        + _DEACTIVATION
        + ",CASE WHEN l.active = 1 AND l.deleted_at IS NULL THEN NULL ELSE last.action END,"
        "CASE WHEN l.active = 1 AND l.deleted_at IS NULL THEN NULL ELSE last.happened_at END,"
        "l.access_transport,l.read_order,l.search_interval_seconds,d.purpose,l.version,"
        "l.origin,l.created_at,l.updated_at "
        "FROM {src}.links l LEFT JOIN last ON last.entity_id=l.id "
        "LEFT JOIN {src}.ingest_daily_routes d ON d.link_id=l.id",
        source,
    )


def _import_settings(db: psycopg.Connection[Any], source: str) -> None:
    db.execute(
        _q(
            "INSERT INTO bm_settings(scope,value,version,updated_at) "
            "SELECT 'search',config,version,updated_at FROM {src}.ingest_settings WHERE id=1",
            source,
        )
    )
    db.execute(
        _q(
            "INSERT INTO bm_settings(scope,value,version,next_run_at,updated_at) "
            "SELECT 'health_' || kind,jsonb_build_object('enabled',enabled,"
            "'interval_minutes',interval_minutes,'workers',workers,'max_attempts',max_attempts,"
            "'bright_data',bright_data,'max_bright_requests',max_bright_requests,"
            "'activate_recovered',activate_recovered,'schedule_hours',to_jsonb(schedule_hours),"
            "'mode',mode,'scope',scope),version,next_run_at,updated_at FROM {src}.health_settings",
            source,
        )
    )


def _count(db: psycopg.Connection[Any], table: sql.Composable) -> int:
    row = db.execute(sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone()
    assert row is not None
    return int(row[0])


def _checksum(
    db: psycopg.Connection[Any], table: sql.Composable, expression: str, order: str
) -> str:
    row = db.execute(
        sql.SQL(
            "SELECT md5(COALESCE(string_agg({expr},'|' ORDER BY {order}),'')) FROM {table}"
        ).format(expr=sql.SQL(expression), order=sql.SQL(order), table=table)
    ).fetchone()
    assert row is not None
    return str(row[0])


def _legacy(source_schema: str, name: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(source_schema), sql.Identifier(name))


def verify(
    db: psycopg.Connection[Any], source_schema: str = LEGACY_SCHEMA
) -> dict[str, tuple[int, int]]:
    """Contagem (origem, destino) por tabela do cadastro."""
    return {
        check.target: (
            _count(db, _legacy(source_schema, check.legacy)),
            _count(db, sql.Identifier(check.target)),
        )
        for check in _CHECKS
    }


def _assert_equal(db: psycopg.Connection[Any], source_schema: str) -> None:
    for target, (source_count, target_count) in verify(db, source_schema).items():
        if source_count != target_count:
            raise ImportMismatchError(f"{target}: origem {source_count}, destino {target_count}")
    for check in _CHECKS:
        source = _checksum(
            db, _legacy(source_schema, check.legacy), check.legacy_expression, check.order
        )
        target = _checksum(db, sql.Identifier(check.target), check.target_expression, check.order)
        if source != target:
            raise ImportMismatchError(f"{check.target}: checksum diferente")
