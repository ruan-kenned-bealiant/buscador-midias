"""Ações automáticas da rotina de saúde sobre o cadastro, com autoria e trilha de auditoria.

O chamador já mantém a trava de escrita do catálogo e a revisão congelada do link; aqui só se
altera o link ou o site na revisão exata que foi testada. A exclusão é lógica (``deleted_at``),
como a da tela. A autoria fica nas colunas ``deactivated_*``: a restauração automática só atua
sobre o que a própria validação excluiu, nunca sobre uma decisão do usuário.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from buscador_midias.catalog.health_rules import Decision
from buscador_midias.db.catalog import _diff, evidence_from_test

_LINK_ACTIONS = {"inactivate": "health_inactivate", "delete": "health_delete"}


def _event(
    db: Any,
    entity: str,
    ident: int,
    action: str,
    before: dict[str, Any],
    after: dict[str, Any],
    **context: Any,
) -> None:
    changes = {**_diff(before, after), **context}
    db.execute(
        "INSERT INTO bm_events(entity,entity_id,action,actor,changes) VALUES(%s,%s,%s,'health',%s)",
        (entity, ident, action, Jsonb(changes)),
    )


def apply_decision(
    db: Any, snapshot: dict[str, Any], run_id: str, decision: Decision
) -> int | None:
    """Aplica ``inactivate`` ou ``delete``; devolve a nova revisão, ou ``None`` se nada mudou."""
    if decision.action not in _LINK_ACTIONS:
        return None
    before = db.execute(
        "SELECT * FROM bm_links WHERE id=%s AND version=%s AND active=1 AND deleted_at IS NULL "
        "FOR UPDATE",
        (snapshot["link_id"], snapshot["link_version"]),
    ).fetchone()
    if not before:
        return None
    after = db.execute(
        "UPDATE bm_links SET active=0,deleted_at=CASE WHEN %s THEN now() END,"
        "deactivated_by='health',deactivated_reason=%s,deactivated_at=now(),"
        "version=version+1,updated_at=now() WHERE id=%s RETURNING *",
        (decision.action == "delete", decision.reason or decision.category, snapshot["link_id"]),
    ).fetchone()
    _event(
        db,
        "endpoint",
        before["id"],
        _LINK_ACTIONS[decision.action],
        before,
        after,
        health_run_id=run_id,
        health_reason=decision.reason,
    )
    return int(after["version"])


def change_transport(db: Any, snapshot: dict[str, Any], run_id: str, transport: str) -> int | None:
    """Passa o link ativo para o transporte que a sonda comprovou; a prova antiga é descartada.

    A prova pertence ao transporte testado: ``last_test``, ``evidence`` e ``read_order`` voltam ao
    estado inicial e serão refeitos no novo transporte.
    """
    before = db.execute(
        "SELECT * FROM bm_links WHERE id=%s AND version=%s AND active=1 AND deleted_at IS NULL "
        "FOR UPDATE",
        (snapshot["link_id"], snapshot["link_version"]),
    ).fetchone()
    if not before or before["access_transport"] == transport:
        return None
    after = db.execute(
        "UPDATE bm_links SET access_transport=%s,read_order='unknown',version=version+1,"
        "updated_at=now() WHERE id=%s RETURNING *",
        (transport, snapshot["link_id"]),
    ).fetchone()
    db.execute(
        "UPDATE bm_link_state SET last_test='{}',evidence='{}',updated_at=now() WHERE link_id=%s",
        (snapshot["link_id"],),
    )
    _event(
        db,
        "endpoint",
        before["id"],
        "health_transport",
        before,
        after,
        health_run_id=run_id,
        health_reason=f"transporte {before['access_transport']} → {transport}",
    )
    return int(after["version"])


def restoration_current(db: Any, snapshot: dict[str, Any]) -> bool:
    """A restauração só vale para o link excluído pela validação, na revisão congelada.

    O site-pai precisa estar ativo, ou ter sido inativado pela validação, ou ter sido reativado
    por esta mesma rodada (a última ação registrada do site prova isso).
    """
    row = db.execute(
        "SELECT l.version,l.deleted_at,l.active,l.deactivated_by,s.version sv,s.active sa,"
        "s.deleted_at sd,s.deactivated_by sby,m.version mv FROM bm_links l "
        "JOIN midias_ia_trusted_sources s ON s.id=l.site_id JOIN bm_mechanisms m ON m.model=l.mechanism "
        "WHERE l.id=%s",
        (snapshot["link_id"],),
    ).fetchone()
    if not row or row["sd"] or not row["deleted_at"] or row["active"]:
        return False
    if (
        row["version"] != snapshot["link_version"]
        or row["mv"] != snapshot["config"]["model_version"]
        or row["deactivated_by"] != "validation"
    ):
        return False
    if row["sv"] == snapshot["source_version"]:
        return bool(row["sa"] or row["sby"] == "validation")
    event = db.execute(
        "SELECT action,changes FROM bm_events WHERE entity='source' AND entity_id=%s "
        "ORDER BY id DESC LIMIT 1",
        (snapshot["source"]["id"],),
    ).fetchone()
    return bool(
        row["sa"]
        and event
        and event["action"] == "health_restore_site"
        and event["changes"].get("version") == [snapshot["source_version"], row["sv"]]
        and event["changes"].get("health_run_id") == snapshot["restore_run_id"]
    )


def activate_recovered(db: Any, snapshot: dict[str, Any], result: dict[str, Any]) -> bool:
    """Só ativa links visíveis de sites ativos, com prova textual; grava o teste como prova."""
    if not result.get("usable_for_news") or not result.get("text_samples"):
        return False
    restore = bool(snapshot.get("restore_run_id"))
    if restore:
        if not restoration_current(db, snapshot):
            return False
        source = db.execute(
            "SELECT * FROM midias_ia_trusted_sources WHERE id=%s FOR UPDATE",
            (snapshot["source"]["id"],),
        ).fetchone()
        if not source["active"]:
            updated = db.execute(
                "UPDATE midias_ia_trusted_sources SET active=true,deactivated_by=NULL,deactivated_reason=NULL,"
                "deactivated_at=NULL,version=version+1,updated_at=now() WHERE id=%s RETURNING *",
                (source["id"],),
            ).fetchone()
            _event(
                db,
                "source",
                source["id"],
                "health_restore_site",
                source,
                updated,
                health_run_id=snapshot["restore_run_id"],
            )
            source = updated
        snapshot["source_version"] = source["version"]
    before = db.execute(
        "SELECT l.* FROM bm_links l JOIN midias_ia_trusted_sources s ON s.id=l.site_id "
        "WHERE l.id=%s AND l.active=0 AND (l.deleted_at IS NULL OR %s) AND s.deleted_at IS NULL "
        "AND s.active FOR UPDATE OF l",
        (snapshot["link_id"], restore),
    ).fetchone()
    if not before:
        return False
    after = db.execute(
        "UPDATE bm_links SET active=1,deleted_at=NULL,deactivated_by=NULL,"
        "deactivated_reason=NULL,deactivated_at=NULL,version=version+1,updated_at=now(),"
        "access_transport=%s,read_order=%s WHERE id=%s RETURNING *",
        (
            result.get("access_transport", "direct"),
            result.get("read_order", "unknown"),
            snapshot["link_id"],
        ),
    ).fetchone()
    db.execute(
        "INSERT INTO bm_link_state(link_id,last_test,evidence) VALUES(%s,%s,%s) "
        "ON CONFLICT (link_id) DO UPDATE SET last_test=EXCLUDED.last_test,"
        "evidence=EXCLUDED.evidence,updated_at=now()",
        (snapshot["link_id"], Jsonb(result), Jsonb(evidence_from_test(result))),
    )
    _event(db, "endpoint", before["id"], "health_reactivate", before, after)
    snapshot["link_version"] = after["version"]
    return True
