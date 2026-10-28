"""Inativação e exclusão automáticas da rotina de saúde, com trilha de auditoria.

Segue o padrão de ``health_activation``: o chamador já mantém a trava de escrita e a revisão
congelada do link; aqui só se altera um link visível e ativo, na revisão exata que foi sondada.
A exclusão é lógica (``deleted_at``), como a da tela, e pode ser revista pela auditoria.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from buscador_midias.catalog.health_rules import Decision
from buscador_midias.catalog.health_store import HealthStore

CHANGES = {
    "inactivate": ("active=0", "health_inactivate"),
    "delete": ("active=0,deleted_at=now()", "health_delete"),
}


def apply_decision(
    db: Any, snapshot: dict[str, Any], run_id: str, decision: Decision
) -> int | None:
    """Aplica ``inactivate`` ou ``delete``; devolve a nova revisão, ou ``None`` se nada mudou."""
    if decision.action not in CHANGES:
        return None
    assignments, event = CHANGES[decision.action]
    before = db.execute(
        "SELECT * FROM links WHERE id=%s AND version=%s AND active=1 AND deleted_at IS NULL "
        "FOR UPDATE",
        (snapshot["link_id"], snapshot["link_version"]),
    ).fetchone()
    if not before:
        return None
    after = db.execute(
        f"UPDATE links SET {assignments},version=version+1,updated_at=now() "  # noqa: S608
        "WHERE id=%s RETURNING *",
        (snapshot["link_id"],),
    ).fetchone()
    audited = dict(HealthStore.jsonable(after), health_run_id=run_id, health_reason=decision.reason)
    db.execute(
        "INSERT INTO events(entity,entity_id,action,happened_at,before_json,after_json) "
        "VALUES('endpoint',%s,%s,now(),%s,%s)",
        (before["id"], event, Jsonb(HealthStore.jsonable(before)), Jsonb(audited)),
    )
    return int(after["version"])
