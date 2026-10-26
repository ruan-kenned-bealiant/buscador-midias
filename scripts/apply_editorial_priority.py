"""Aplica provas editoriais recentes da rodada, com revisão e auditoria por fonte."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from buscador_midias.catalog.editorial_mapping import normalized, purpose, recent_samples
from buscador_midias.catalog.health_restoration import automatic_event
from buscador_midias.catalog.postgres import WRITE_LOCK, connect_postgres
from buscador_midias.catalog.store import CatalogStore
from map_editorial_priority import OUT, credentials, dict_row, stamp, write

RUN = "prioritarios-editoriais-20261026"


def json_value(value):
    """Serializa revisões completas, incluindo horários do PostgreSQL."""
    return Jsonb(value, dumps=lambda data: json.dumps(data, default=str, ensure_ascii=False))


def event(db, entity, ident, action, before, after):
    """Registra revisões antes/depois e a rodada responsável pela alteração."""
    db.execute(
        "INSERT INTO midias_manager.events(entity,entity_id,action,happened_at,"
        "before_json,after_json) VALUES(%s,%s,%s,now(),%s,%s)",
        (entity, ident, action, json_value(before), json_value(dict(after, mapping_run=RUN))),
    )


def apply_source(source, tests, manifest, aliases):
    """Uma transação por fonte; nunca restaura exclusões manuais nem revisões novas."""
    decisions = []
    frozen = {link["id"]: link for link in manifest["links"]}
    with connect_postgres(credentials()) as db:
        db.row_factory = dict_row
        db.execute("SET LOCAL search_path TO midias_manager,pg_catalog")
        db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
        parent = db.execute(
            "SELECT * FROM sites WHERE id=%s FOR UPDATE", (source["id"],)
        ).fetchone()
        if not parent or parent["deleted_at"] or parent["version"] != source["version"]:
            return [dict(action="revision_preserved", source_id=source["id"])]
        own = {
            link["url"]: link
            for link in db.execute(
                "SELECT * FROM links WHERE source_id=%s FOR UPDATE", (source["id"],)
            ).fetchall()
        }
        visible = {
            normalized(link["url"]): link
            for link in db.execute(
                "SELECT l.id,l.source_id,l.url FROM links l JOIN sites s ON s.id=l.source_id "
                "WHERE l.deleted_at IS NULL AND s.deleted_at IS NULL"
            ).fetchall()
        }
        # Um alias observado nesta rodada também pode já estar cadastrado.
        for url, target in aliases.items():
            if normalized(url) in visible:
                visible.setdefault(normalized(target), visible[normalized(url)])
        models = {m["model"]: m for m in db.execute("SELECT * FROM mechanisms").fetchall()}
        changed = False
        for item in tests:
            result = item["result"]
            url = result["url"]
            decision = dict(
                source_id=source["id"], source=source["name"], url=url, test_id=result["test_id"]
            )
            if not result.get("usable_for_news") or not recent_samples(result):
                continue
            before = own.get(url)
            duplicate = visible.get(normalized(aliases.get(url, url))) or visible.get(
                normalized(url)
            )
            if duplicate and (not before or duplicate["id"] != before["id"]):
                decisions.append(
                    dict(decision, action="duplicate_preserved", link_id=duplicate["id"])
                )
                continue
            if result["model_version"] != models[result["model"]]["version"]:
                decisions.append(dict(decision, action="model_revision_preserved"))
                continue
            if result["source_url"] != parent["url"]:
                decisions.append(dict(decision, action="source_url_preserved"))
                continue
            route = purpose(result)
            if route != "updates":
                decisions.append(dict(decision, action="archive_or_index_review", purpose=route))
                continue
            if before:
                previous = frozen.get(before["id"])
                if not previous or before["version"] != previous["version"]:
                    decisions.append(dict(decision, action="link_revision_preserved"))
                    continue
                if before["deleted_at"] and not automatic_event(
                    db, "endpoint", before["id"], "validation_exclude", before["version"]
                ):
                    decisions.append(dict(decision, action="manual_deletion_preserved"))
                    continue
                latest = db.execute(
                    "SELECT action FROM events WHERE entity='endpoint' "
                    "AND entity_id=%s ORDER BY id DESC LIMIT 1",
                    (before["id"],),
                ).fetchone()
                if (
                    not before["active"]
                    and not before["deleted_at"]
                    and latest
                    and latest["action"] == "update"
                ):
                    decisions.append(dict(decision, action="manual_inactivation_preserved"))
                    continue
                after = db.execute(
                    "UPDATE links SET deleted_at=NULL,active=1,model=%s,kind=%s,last_test=%s,"
                    "evidence=%s,version=version+1,updated_at=now() WHERE id=%s RETURNING *",
                    (
                        result["model"],
                        result["requested_kind"],
                        json_value(result),
                        json_value(CatalogStore._test_evidence(result)),
                        before["id"],
                    ),
                ).fetchone()
                action = (
                    "editorial_mapping_restore"
                    if before["deleted_at"] or not before["active"]
                    else "editorial_mapping_verify"
                )
            else:
                after = db.execute(
                    "INSERT INTO links(source_id,url,kind,model,label,notes,active,created_at,"
                    "updated_at,evidence,last_test,origin,search_interval_seconds) "
                    "VALUES(%s,%s,%s,%s,%s,%s,1,now(),now(),%s,%s,%s,14400) RETURNING *",
                    (
                        source["id"],
                        url,
                        result["requested_kind"],
                        result["model"],
                        "Notícias editoriais · " + urlsplit(url).path[-150:],
                        "Acesso, artigo e texto recente comprovados em amostra HTTP em 26/10/2026. "
                        "Não representa cobertura integral. " + item.get("provenance", ""),
                        json_value(CatalogStore._test_evidence(result)),
                        json_value(result),
                        RUN,
                    ),
                ).fetchone()
                action = "editorial_mapping_create"
            db.execute(
                "INSERT INTO link_test_results(link_id,model,test_id,model_version,observed_at,"
                "parameters,result) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    after["id"],
                    result["model"],
                    result["test_id"],
                    result["model_version"],
                    result["observed_at"],
                    json_value(result["parameters"]),
                    json_value(result),
                ),
            )
            event(db, "endpoint", after["id"], action, before, after)
            route_before = db.execute(
                "SELECT * FROM ingest_daily_routes WHERE link_id=%s", (after["id"],)
            ).fetchone()
            # Uma classificação explícita prévia é preservada para revisão, inclusive 'archive'.
            if route_before is None:
                route_after = db.execute(
                    "INSERT INTO ingest_daily_routes(link_id,purpose,reason) "
                    "VALUES(%s,'updates',%s) RETURNING *",
                    (
                        after["id"],
                        "Notícia editorial com data e texto confirmados nesta rodada; "
                        "prova amostral.",
                    ),
                ).fetchone()
                event(
                    db, "endpoint", after["id"], "editorial_mapping_daily_route", None, route_after
                )
            decisions.append(
                dict(
                    decision,
                    action=action,
                    link_id=after["id"],
                    purpose=route_before["purpose"] if route_before else "updates",
                )
            )
            visible[normalized(url)] = after
            visible[normalized(aliases.get(url, url))] = after
            own[url] = after
            # Uma segunda amostra do mesmo endereço não reaplica o cadastro.
            frozen[after["id"]] = after
            changed = True
        if changed and not parent["active"]:
            created_here = (
                source["id"] == 1295
                and parent["url"] == "https://www.cnn.com"
                and (parent["notes"].startswith("Auditoria editorial prioritária de 26/10/2026"))
            )
            if created_here or automatic_event(
                db, "source", parent["id"], "validation_inactivate", parent["version"]
            ):
                after = db.execute(
                    "UPDATE sites SET active=1,version=version+1,updated_at=now() "
                    "WHERE id=%s RETURNING *",
                    (parent["id"],),
                ).fetchone()
                event(db, "source", parent["id"], "editorial_mapping_activate", parent, after)
                decisions.append(dict(source_id=parent["id"], action="source_activated"))
            else:
                decisions.append(
                    dict(source_id=parent["id"], action="manual_source_state_preserved")
                )
    return decisions


def main():
    """Agrupa testes por fonte e exige opção explícita para aplicar."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((OUT / "manifest.json").read_text())
    data = json.loads((OUT / "resultado.json").read_text())["sources"]
    data += json.loads((OUT / "complemento.json").read_text())
    grouped = defaultdict(list)
    sources, aliases = {}, {}
    for row in data:
        sources[row["source"]["id"]] = row["source"]
        grouped[row["source"]["id"]].extend(row["results"])
        aliases.update(
            {r["url"]: r["final_url"] for r in row.get("responses", []) if r.get("final_url")}
        )
    decisions = []
    for ident, tests in grouped.items():
        path = OUT / "aplicados" / f"{ident}.json"
        if path.exists():
            decisions.extend(json.loads(path.read_text()))
        elif args.apply:
            result = apply_source(sources[ident], tests, manifest, aliases)
            write(path, result)
            decisions.extend(result)
        else:
            print(sources[ident]["name"], len([r for r in tests if recent_samples(r["result"])]))
    if args.apply:
        write(OUT / "aplicacao.json", dict(finished_at=stamp(), decisions=decisions))
        from collections import Counter

        print(dict(Counter(r["action"] for r in decisions)))


if __name__ == "__main__":
    main()
