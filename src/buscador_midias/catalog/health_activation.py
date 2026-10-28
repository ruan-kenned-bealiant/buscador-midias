"""Ativação explícita de recuperados, com prova textual e trilha de auditoria."""

from typing import Any

from psycopg.types.json import Jsonb

from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.store import CatalogStore


def activate_recovered(db: Any, snapshot: dict[str, Any], result: dict[str, Any]) -> bool:
    """Só ativa links visíveis de sites ativos; o chamador já mantém trava e revisões."""
    if not result.get("usable_for_news") or not result.get("text_samples"):
        return False
    restore = bool(snapshot.get("restore_run_id"))
    if restore:
        from buscador_midias.catalog.health_restoration import restoration_current

        if not restoration_current(db, snapshot):
            return False
        source = db.execute(
            "SELECT * FROM sites WHERE id=%s FOR UPDATE", (snapshot["source"]["id"],)
        ).fetchone()
        if not source["active"]:
            updated = db.execute(
                "UPDATE sites SET active=1,version=version+1,updated_at=now() "
                "WHERE id=%s RETURNING *",
                (source["id"],),
            ).fetchone()
            audited = dict(HealthStore.jsonable(updated), health_run_id=snapshot["restore_run_id"])
            db.execute(
                "INSERT INTO events(entity,entity_id,action,happened_at,before_json,after_json) "
                "VALUES('source',%s,'health_restore_site',now(),%s,%s)",
                (source["id"], Jsonb(HealthStore.jsonable(source)), Jsonb(audited)),
            )
            source = updated
        snapshot["source_version"] = source["version"]
    before = db.execute(
        "SELECT l.* FROM links l JOIN sites s ON s.id=l.source_id "
        "WHERE l.id=%s AND l.active=0 AND (l.deleted_at IS NULL OR %s) AND s.deleted_at IS NULL "
        "AND s.active=1 FOR UPDATE OF l",
        (snapshot["link_id"], restore),
    ).fetchone()
    if not before:
        return False
    after = db.execute(
        "UPDATE links SET active=1,deleted_at=NULL,version=version+1,updated_at=now(),"
        "last_test=%s,evidence=%s,access_transport=%s,read_order=%s WHERE id=%s RETURNING *",
        (
            Jsonb(result),
            Jsonb(CatalogStore._test_evidence(result)),
            result.get("access_transport", "direct"),
            result.get("read_order", "unknown"),
            snapshot["link_id"],
        ),
    ).fetchone()
    db.execute(
        "INSERT INTO link_test_results(link_id,model,test_id,model_version,"
        "observed_at,parameters,result) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (
            before["id"],
            result["model"],
            result["test_id"],
            result["model_version"],
            result["observed_at"],
            Jsonb(result["parameters"]),
            Jsonb(result),
        ),
    )
    db.execute(
        "INSERT INTO events(entity,entity_id,action,happened_at,before_json,after_json) "
        "VALUES('endpoint',%s,'health_reactivate',now(),%s,%s)",
        (before["id"], Jsonb(HealthStore.jsonable(before)), Jsonb(HealthStore.jsonable(after))),
    )
    snapshot["link_version"] = after["version"]
    return True
