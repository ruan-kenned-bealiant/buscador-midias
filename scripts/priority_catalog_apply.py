"""Aplica apenas provas textuais da rodada dirigida, preservando exclusões manuais."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from buscador_midias.catalog.health_restoration import automatic_event
from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.postgres import WRITE_LOCK, PostgresCatalogStore
from buscador_midias.catalog.store import CatalogStore

OUT = Path(__file__).resolve().parents[1] / "var/gerenciador/mapeamento-prioritario"
RUN = "mapeamento-prioritario-20261024"


def normalized(url):
    """Deduplica diferenças de www, esquema e barra final; mantém consultas distintas."""
    p = urlsplit(url)
    return ((p.hostname or "").removeprefix("www."), p.path.rstrip("/"), p.query)


def audit(db, repository, entity, ident, action, before, after):
    """Guarda as duas revisões sem apagar o evento original de exclusão."""
    db.execute(
        "INSERT INTO events(entity,entity_id,action,happened_at,before_json,after_json) "
        "VALUES(%s,%s,%s,now(),%s,%s)",
        (
            entity,
            ident,
            action,
            Jsonb(repository.jsonable(before)) if before else None,
            Jsonb(dict(repository.jsonable(after), mapping_run=RUN)),
        ),
    )


def apply_source(repository, data):
    """Uma transação por fonte e apenas resultados positivos, ainda compatíveis."""
    source = data["source"]
    decisions = []
    with repository.connection() as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
        parent = db.execute(
            "SELECT * FROM sites WHERE id=%s FOR UPDATE", (source["id"],)
        ).fetchone()
        if not parent or parent["deleted_at"] or parent["version"] != source["version"]:
            return [
                dict(
                    source_id=source["id"], action="skipped", reason="Fonte alterada desde o teste"
                )
            ]
        auto_inactive = not parent["active"] and automatic_event(
            db, "source", parent["id"], "validation_inactivate", parent["version"]
        )
        proposed = data.get("proposed_source_url")
        if proposed:
            # O feed oficial da BBC publica seus artigos em bbc.co.uk/news.
            # Corrige o pai somente após prova textual nesse domínio e com revisão intacta.
            positive = any(
                r["result"].get("usable_for_news") and r["result"].get("source_url") == proposed
                for r in data["results"]
            )
            if (
                not positive
                or not auto_inactive
                or parent["url"] != "https://www.bbc.com/news"
                or proposed != "https://www.bbc.co.uk/news"
            ):
                return [
                    dict(
                        source_id=parent["id"],
                        action="skipped",
                        reason="Mudança de domínio sem prova ou fora do caso oficial BBC",
                    )
                ]
            identity = "bbc.co.uk/news"
            if db.execute(
                "SELECT 1 FROM sites WHERE identity=%s AND id<>%s", (identity, parent["id"])
            ).fetchone():
                return [
                    dict(
                        source_id=parent["id"],
                        action="skipped",
                        reason="Domínio oficial já cadastrado em outra fonte",
                    )
                ]
            updated = db.execute(
                "UPDATE sites SET "
                "url=%s,identity=%s,version=version+1,updated_at=now() WHERE id=%s "
                "RETURNING *",
                (proposed, identity, parent["id"]),
            ).fetchone()
            audit(
                db,
                repository,
                "source",
                parent["id"],
                "priority_mapping_official_domain",
                parent,
                updated,
            )
            decisions.append(
                dict(
                    source_id=parent["id"],
                    source=parent["name"],
                    action="source_official_domain",
                    url=proposed,
                )
            )
            parent = updated
        existing = db.execute("SELECT * FROM links WHERE source_id=%s", (source["id"],)).fetchall()
        visible = {
            normalized(r["url"]): r
            for r in db.execute(
                "SELECT l.id,l.source_id,l.url FROM links l JOIN sites s ON s.id=l.source_id "
                "WHERE l.active=1 AND l.deleted_at IS NULL AND s.deleted_at IS NULL"
            ).fetchall()
        }
        own = {r["url"]: r for r in existing if r["source_id"] == source["id"]}
        changed = False
        for item in data["results"]:
            result = item["result"]
            if not result.get("usable_for_news") or not result.get("text_samples"):
                continue
            decision = dict(
                source_id=source["id"],
                source=source["name"],
                url=result["url"],
                test_id=result["test_id"],
            )
            parsed = urlsplit(result["url"])
            if (
                re.search(r"(?:19|20)\d{2}", parsed.path)
                or re.search(r"(?:yyyy|year)=", parsed.query)
                or "webstories" in parsed.path.lower()
            ):
                decision.update(
                    action="archive_verified_not_added",
                    reason=("Arquivo datado ou formato de apresentação; "
                            "prova preservada fora do cadastro de atualização"),
                )
                decisions.append(decision)
                continue
            before = own.get(result["url"])
            duplicate = visible.get(normalized(result["url"]))
            if duplicate:
                decision.update(action="already_present", link_id=duplicate["id"])
                decisions.append(decision)
                continue
            current_model = db.execute(
                "SELECT version FROM mechanisms WHERE model=%s", (result["model"],)
            ).fetchone()
            if (
                not current_model
                or current_model["version"] != result["model_version"]
                or result["source_url"] != parent["url"]
            ):
                decision.update(action="skipped", reason="Configuração mudou")
                decisions.append(decision)
                continue
            if before:
                if before["updated_at"] > datetime.fromisoformat(result["observed_at"]):
                    decision.update(action="skipped", reason="Link alterado durante o teste")
                    decisions.append(decision)
                    continue
                event = db.execute(
                    "SELECT action FROM events WHERE entity='endpoint' AND "
                    "entity_id=%s ORDER BY id DESC LIMIT 1",
                    (before["id"],),
                ).fetchone()
                if before["deleted_at"] and not automatic_event(
                    db, "endpoint", before["id"], "validation_exclude", before["version"]
                ):
                    decision.update(action="preserved_manual", reason="Exclusão não automática")
                    decisions.append(decision)
                    continue
                if (
                    not before["deleted_at"]
                    and not before["active"]
                    and event
                    and event["action"] == "update"
                ):
                    decision.update(action="preserved_manual", reason="Inativação manual")
                    decisions.append(decision)
                    continue
                after = db.execute(
                    "UPDATE links SET "
                    "deleted_at=NULL,active=1,model=%s,kind=%s,last_test=%s,evidence=%s,"
                    "version=version+1,updated_at=now() WHERE id=%s RETURNING *",
                    (
                        result["model"],
                        result["requested_kind"],
                        Jsonb(result),
                        Jsonb(CatalogStore._test_evidence(result)),
                        before["id"],
                    ),
                ).fetchone()
                action = "priority_mapping_restore"
            else:
                after = db.execute(
                    "INSERT INTO "
                    "links(source_id,url,kind,model,label,notes,active,created_at,updated_at,"
                    "evidence,last_test,origin,search_interval_seconds) "
                    "VALUES(%s,%s,%s,%s,%s,%s,1,now(),now(),%s,%s,%s,14400) RETURNING "
                    "*",
                    (
                        source["id"],
                        result["url"],
                        result["requested_kind"],
                        result["model"],
                        urlsplit(result["url"]).path.rsplit("/", 1)[-1][:200],
                        "Mapeamento dirigido: " + item["provenance"],
                        Jsonb(CatalogStore._test_evidence(result)),
                        Jsonb(result),
                        RUN,
                    ),
                ).fetchone()
                action = "priority_mapping_create"
            db.execute(
                "INSERT INTO "
                "link_test_results(link_id,model,test_id,model_version,observed_at,parameters,result)"
                " "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    after["id"],
                    result["model"],
                    result["test_id"],
                    result["model_version"],
                    result["observed_at"],
                    Jsonb(result["parameters"]),
                    Jsonb(result),
                ),
            )
            audit(db, repository, "endpoint", after["id"], action, before, after)
            decision.update(action=action, link_id=after["id"], text_samples=result["text_samples"])
            decisions.append(decision)
            visible[normalized(result["url"])] = after
            own[result["url"]] = after
            changed = True
        if changed and not parent["active"]:
            if auto_inactive:
                updated = db.execute(
                    "UPDATE sites SET active=1,version=version+1,updated_at=now() "
                    "WHERE id=%s RETURNING *",
                    (parent["id"],),
                ).fetchone()
                audit(
                    db,
                    repository,
                    "source",
                    parent["id"],
                    "priority_mapping_activate",
                    parent,
                    updated,
                )
                decisions.append(
                    dict(source_id=parent["id"], source=parent["name"], action="source_activated")
                )
            else:
                decisions.append(dict(source_id=parent["id"], action="source_preserved_manual"))
    return decisions


def main():
    """Lê checkpoints concluídos; um arquivo de aplicação impede repetição da fonte."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    repository = HealthStore(PostgresCatalogStore())
    targets = json.loads((OUT / "alvos.json").read_text())["alvos"]
    all_decisions = []
    for source in targets:
        path = OUT / "testes" / f"{source['id']}.json"
        applied = OUT / "aplicados" / f"{source['id']}.json"
        if applied.exists():
            all_decisions.extend(json.loads(applied.read_text()))
            continue
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        if not data.get("completed"):
            continue
        if args.apply:
            decisions = apply_source(repository, data)
            applied.parent.mkdir(parents=True, exist_ok=True)
            applied.write_text(json.dumps(decisions, ensure_ascii=False, indent=2) + "\n")
            all_decisions.extend(decisions)
        else:
            print(
                source["name"],
                [r["url"] for r in data["results"] if r["result"]["usable_for_news"]],
            )
    if args.apply:
        from collections import Counter

        print(dict(Counter(r["action"] for r in all_decisions)))


if __name__ == "__main__":
    main()
