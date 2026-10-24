"""Proveniência e revisões exigidas para recuperar exclusões automáticas."""

from typing import Any


def automatic_event(db: Any, entity: str, ident: int, action: str, version: int) -> bool:
    """A última alteração deve ser a exclusão/inativação automática desta revisão."""
    event = db.execute(
        "SELECT action,after_json FROM events WHERE entity=%s AND entity_id=%s "
        "ORDER BY id DESC LIMIT 1",
        (entity, ident),
    ).fetchone()
    return bool(
        event
        and event["action"] == action
        and (event["after_json"] or {}).get("version") == version
    )


def restoration_current(db: Any, snapshot: dict[str, Any]) -> bool:
    """Aceita apenas a revisão congelada ou uma reativação do pai feita nesta rodada."""
    row = db.execute(
        "SELECT l.version,l.deleted_at,l.active,s.version sv,s.active sa,s.deleted_at sd,"
        "m.version mv FROM links l JOIN sites s ON s.id=l.source_id "
        "JOIN mechanisms m ON m.model=l.model WHERE l.id=%s",
        (snapshot["link_id"],),
    ).fetchone()
    if not row or row["sd"] or not row["deleted_at"] or row["active"]:
        return False
    if (
        row["version"] != snapshot["link_version"]
        or row["mv"] != snapshot["config"]["model_version"]
    ):
        return False
    if not automatic_event(
        db, "endpoint", snapshot["link_id"], "validation_exclude", row["version"]
    ):
        return False
    if row["sv"] == snapshot["source_version"]:
        return bool(
            row["sa"]
            or automatic_event(
                db, "source", snapshot["source"]["id"], "validation_inactivate", row["sv"]
            )
        )
    event = db.execute(
        "SELECT action,before_json,after_json FROM events WHERE entity='source' "
        "AND entity_id=%s ORDER BY id DESC LIMIT 1",
        (snapshot["source"]["id"],),
    ).fetchone()
    return bool(
        row["sa"]
        and event
        and event["action"] == "health_restore_site"
        and event["before_json"].get("version") == snapshot["source_version"]
        and event["after_json"].get("version") == row["sv"]
        and event["after_json"].get("health_run_id") == snapshot["restore_run_id"]
    )
