"""Restaura apenas provas textuais independentes, com transporte e revisão auditados."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from buscador_midias.catalog.editorial_mapping import normalized, purpose
from buscador_midias.catalog.http_transport import validate_transport
from buscador_midias.catalog.postgres import WRITE_LOCK, connect_postgres
from buscador_midias.catalog.store import CatalogStore
from buscador_midias.domain.site import Site
from map_editorial_priority import credentials
from recover_all_transports import OUT, read_gz, stamp, write_gz


def as_json(value):
    """Serializa horários sem incluir dados de sessão."""
    return Jsonb(value, dumps=lambda data: json.dumps(data, ensure_ascii=False, default=str))


def route_purpose(result):
    """Preserva arquivos e grandes índices fora da busca diária até calibração própria."""
    route = purpose(result)
    if route != "updates":
        return route
    samples = [s for s in result["samples"] if s.get("text") and s.get("article")]
    dates = []
    for sample in samples:
        try:
            if sample.get("published_at"):
                dates.append(datetime.fromisoformat(sample["published_at"]))
        except ValueError:
            pass
    if not dates:
        return "review"
    if all(d < datetime.now(UTC) - timedelta(days=365) for d in dates):
        return "archive"
    return "updates"


def eligible(row):
    """Não aceita somente XML, áudio ou confirmação redirecionada para fora do veículo."""
    if not row.get("confirmed") or not row.get("result", {}).get("usable_for_news"):
        return False
    confirmation = row.get("confirmation", {})
    audit = confirmation.get("audit", {})
    if not (audit.get("texto_util_confirmado") and audit.get("artigo_acessivel")) or audit.get(
        "audio_dedicado"
    ):
        return False
    item = row["link"]
    site = Site.create(
        name=item["source_name"],
        url=item["source_url"],
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )
    final = (confirmation.get("events") or [{}])[-1].get("url", confirmation.get("url", ""))
    return site.owns(final)


def compact(row):
    """Eventos referenciam o teste completo preservado em link_test_results."""
    keys = (
        "id",
        "source_id",
        "url",
        "active",
        "deleted_at",
        "model",
        "kind",
        "version",
        "access_transport",
    )
    return {**{k: row.get(k) for k in keys}, "test_id": (row.get("last_test") or {}).get("test_id")}


def audit_event(db, entity, ident, action, before, after, run):
    """Registra toda alteração atribuída a esta rodada."""
    db.execute(
        "INSERT INTO "
        "midias_manager.events(entity,entity_id,action,happened_at,before_json,after_json)"
        " VALUES(%s,%s,%s,now(),%s,%s)",
        (entity, ident, action, as_json(before), as_json(dict(after, transport_recovery_run=run))),
    )


def archive(db, ident, result):
    """Mantém testes anteriores e novos imutáveis e relacionados ao link."""
    if not result.get("test_id"):
        return
    db.execute(
        "INSERT INTO "
        "midias_manager.link_test_results(link_id,model,test_id,model_version,observed_at,parameters,result)"
        " VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (
            ident,
            result["model"],
            result["test_id"],
            result.get("model_version"),
            result["observed_at"],
            as_json(result.get("parameters", {})),
            as_json(result),
        ),
    )


def apply_row(db, row, manifest, visible, models):
    """Aplica um resultado só se a revisão original ou uma restauração própria ainda vale."""
    item = row["link"]
    result = dict(row["result"], independent_confirmation=row["confirmation"])
    profile = validate_transport(row["profile"])
    ident = item["id"]
    run = manifest["id"]
    decision = dict(link_id=ident, source_id=item["source_id"], url=item["url"], profile=profile)
    if not eligible(row):
        return dict(decision, action="not_confirmed")
    if models[result["model"]] != result["model_version"]:
        return dict(decision, action="model_changed")
    parent = db.execute(
        "SELECT * FROM midias_manager.sites WHERE id=%s FOR UPDATE", (item["source_id"],)
    ).fetchone()
    if not parent or parent["deleted_at"]:
        return dict(decision, action="source_deleted")
    source_event = db.execute(
        "SELECT action,after_json FROM midias_manager.events WHERE entity='source' AND "
        "entity_id=%s ORDER BY id DESC LIMIT 1",
        (parent["id"],),
    ).fetchone()
    own_restore = bool(
        source_event
        and source_event["action"] == "transport_restore_source"
        and (source_event["after_json"] or {}).get("transport_recovery_run") == run
        and (source_event["after_json"] or {}).get("version") == parent["version"]
    )
    if parent["url"] != item["source_url"] or (
        parent["version"] != item["source_version"] and not own_restore
    ):
        return dict(decision, action="source_changed")
    if not parent["active"] and (
        not source_event or source_event["action"] != "validation_inactivate"
    ):
        return dict(decision, action="manual_source_preserved")
    before = (
        db.execute("SELECT * FROM midias_manager.links WHERE id=%s FOR UPDATE", (ident,)).fetchone()
        if ident > 0
        else None
    )
    if before and (before["version"] != item["version"] or before["url"] != item["url"]):
        return dict(decision, action="link_changed")
    if before and before["deleted_at"]:
        last = db.execute(
            "SELECT action FROM midias_manager.events WHERE entity='endpoint' AND "
            "entity_id=%s ORDER BY id DESC LIMIT 1",
            (ident,),
        ).fetchone()
        if not last or last["action"] != "validation_exclude":
            return dict(decision, action="manual_deletion_preserved")
    key = normalized(item["url"])
    duplicate = visible.get(key)
    if duplicate and duplicate != ident:
        return dict(decision, action="duplicate_preserved", duplicate_link_id=duplicate)
    if before:
        archive(db, ident, before["last_test"] or {})
        after = db.execute(
            "UPDATE midias_manager.links SET "
            "active=1,deleted_at=NULL,model=%s,kind=%s,access_transport=%s,last_test=%s,evidence=%s,version=version+1,updated_at=now()"
            " WHERE id=%s RETURNING *",
            (
                result["model"],
                result["requested_kind"],
                profile,
                as_json(result),
                as_json(CatalogStore._test_evidence(result)),
                ident,
            ),
        ).fetchone()
        action = "restored" if before["deleted_at"] else "transport_configured"
    else:
        after = db.execute(
            "INSERT INTO "
            "midias_manager.links(source_id,url,kind,model,access_transport,label,notes,active,created_at,updated_at,last_test,evidence,origin,search_interval_seconds)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,1,now(),now(),%s,%s,%s,14400) RETURNING *",
            (
                item["source_id"],
                item["url"],
                result["requested_kind"],
                result["model"],
                profile,
                "Notícias · transporte validado",
                "XML, notícia e texto comprovados em duas sessões independentes. Rodada de "
                "recuperação HTTP de 26/10/2026.",
                as_json(result),
                as_json(CatalogStore._test_evidence(result)),
                "transport-recovery-" + run,
            ),
        ).fetchone()
        ident = after["id"]
        decision["link_id"] = ident
        action = "created"
    archive(db, ident, result)
    audit_event(
        db,
        "endpoint",
        ident,
        "transport_recovery",
        compact(before) if before else None,
        compact(after),
        run,
    )
    current_route = db.execute(
        "SELECT * FROM midias_manager.ingest_daily_routes WHERE link_id=%s", (ident,)
    ).fetchone()
    route = current_route["purpose"] if current_route else route_purpose(result)
    if not current_route:
        new_route = db.execute(
            "INSERT INTO midias_manager.ingest_daily_routes(link_id,purpose,reason) "
            "VALUES(%s,%s,%s) RETURNING *",
            (
                ident,
                route,
                "Recuperação de transporte com prova textual independente; arquivos e índices "
                "extensos separados da atualização diária.",
            ),
        ).fetchone()
        audit_event(db, "endpoint", ident, "transport_recovery_route", None, new_route, run)
    if not parent["active"]:
        updated = db.execute(
            "UPDATE midias_manager.sites SET active=1,version=version+1,updated_at=now() "
            "WHERE id=%s RETURNING *",
            (parent["id"],),
        ).fetchone()
        audit_event(db, "source", parent["id"], "transport_restore_source", parent, updated, run)
        decision["source_activated"] = True
    visible[key] = ident
    return dict(decision, action=action, purpose=route, test_id=result["test_id"])


def main():
    """Uma aplicação finita dos checkpoints disponíveis, sem iniciar outra busca."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest = read_gz(OUT / "manifest.json.gz")
    paths = sorted((OUT / "results").glob("*.json.gz"))
    pending = []
    for path in paths:
        target = OUT / "applied" / path.name
        if target.exists():
            continue
        row = read_gz(path)
        if eligible(row):
            pending.append(row)
    print(stamp(), "PROVAS PENDENTES", len(pending), flush=True)
    if not args.apply:
        return
    with connect_postgres(credentials()) as db:
        db.row_factory = dict_row
        visible = {
            normalized(r["url"]): r["id"]
            for r in db.execute(
                "SELECT l.id,l.url FROM midias_manager.links l JOIN midias_manager.sites s ON "
                "s.id=l.source_id WHERE l.deleted_at IS NULL AND s.deleted_at IS NULL"
            )
        }
        models = {
            r["model"]: r["version"]
            for r in db.execute("SELECT model,version FROM midias_manager.mechanisms")
        }
    counts = Counter()
    for position in range(0, len(pending), 50):
        batch = pending[position : position + 50]
        decisions = []
        with connect_postgres(credentials()) as db:
            db.row_factory = dict_row
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            models = {
                r["model"]: r["version"]
                for r in db.execute("SELECT model,version FROM midias_manager.mechanisms")
            }
            for row in batch:
                decisions.append((row["link"]["id"], apply_row(db, row, manifest, visible, models)))
        for original_id, decision in decisions:
            write_gz(OUT / "applied" / f"{original_id}.json.gz", decision)
            counts[decision["action"]] += 1
        print(stamp(), position + len(batch), dict(counts), flush=True)
    print(stamp(), "APLICAÇÃO CONCLUÍDA", dict(counts), flush=True)


if __name__ == "__main__":
    main()
