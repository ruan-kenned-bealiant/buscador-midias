"""Consolidação explícita das versões estaduais do g1, preservando a origem."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from buscador_midias.catalog.store import CatalogError, CatalogStore, _stamp


def regional_plan(store: CatalogStore) -> list[dict[str, Any]]:
    """Seleciona apenas estados do g1 sob o mesmo domínio e cadastro principal."""
    with store.connection() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM sites WHERE deleted_at IS NULL")]
        parents = [r for r in rows if r["url"] == "https://g1.globo.com"]
        if len(parents) != 1:
            return []
        parent = parents[0]
        result = []
        for child in rows:
            parts = urlsplit(child["url"])
            path = parts.path.strip("/").split("/")
            if (
                child["name"].startswith("g1 ")
                and parts.hostname == "g1.globo.com"
                and len(path) == 2
                and path[0]
                in {
                    "ac",
                    "al",
                    "ap",
                    "am",
                    "ba",
                    "ce",
                    "df",
                    "es",
                    "go",
                    "ma",
                    "mt",
                    "ms",
                    "mg",
                    "pa",
                    "pb",
                    "pr",
                    "pe",
                    "pi",
                    "rj",
                    "rn",
                    "rs",
                    "ro",
                    "rr",
                    "sc",
                    "sp",
                    "se",
                    "to",
                }
            ):
                result.append(
                    dict(
                        source_id=child["id"],
                        version=child["version"],
                        name=child["name"],
                        url=child["url"],
                        target_id=parent["id"],
                        target_version=parent["version"],
                    )
                )
        return result


def consolidate_regionals(store: CatalogStore, plan: list[dict[str, Any]]) -> dict[str, int]:
    """Aplica um plano revisado atomicamente; não apaga provas nem sobrescreve o destino."""
    return _consolidate(store, plan, lambda: regional_plan(store), "consolidation_g1")


def consolidate_reviewed(store: CatalogStore, plan: list[dict[str, Any]]) -> dict[str, int]:
    """Aplica somente as identidades e seções revisadas explicitamente na auditoria."""
    from buscador_midias.catalog.review import reviewed_plan

    return _consolidate(store, plan, lambda: reviewed_plan(store), "consolidation_review")


def _consolidate(
    store: CatalogStore,
    plan: list[dict[str, Any]],
    expected: Callable[[], list[dict[str, Any]]],
    metadata_key: str,
) -> dict[str, int]:
    counts = dict(sources=0, added_links=0, shared_links=0, labeled_links=0)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        # Recusa planos antigos e fontes fora da regra, antes de qualquer mutação.
        current = expected()
        if plan != current:
            raise CatalogError("O catálogo mudou. Gere novamente o plano de consolidação.", 409)
        for item in plan:
            source = store._record(db, "sites", item["source_id"])
            target = store._record(db, "sites", item["target_id"])
            endpoints = db.execute(
                "SELECT * FROM links WHERE source_id=? AND deleted_at IS NULL",
                (source["id"],),
            ).fetchall()
            now = _stamp()
            if item.get("reason"):
                annotation = f"Cadastro incorporado: {source['name']} ({source['url']})."
                notes = (target["notes"] + "\n" + annotation).strip()
                if len(notes) > 4000:
                    raise CatalogError("Observações de destino excederiam o limite; revise.", 409)
                db.execute(
                    "UPDATE sites SET notes=?,version=version+1,updated_at=? WHERE id=?",
                    (notes, now, target["id"]),
                )
                updated = dict(
                    db.execute("SELECT * FROM sites WHERE id=?", (target["id"],)).fetchone()
                )
                store._event(db, "source", target["id"], "update", target, updated)
            for row in endpoints:
                old = dict(row)
                existing = db.execute(
                    "SELECT * FROM links WHERE source_id=? AND url=?",
                    (target["id"], old["url"]),
                ).fetchone()
                if existing is not None:
                    if existing["deleted_at"] is not None:
                        raise CatalogError(
                            "Um link de destino foi excluído; revise o conflito.", 409
                        )
                    counts["shared_links"] += 1
                    # Só o RSS estadual recebe o nome da seção; XMLs gerais não.
                    suffix = urlsplit(source["url"]).path.rstrip("/")
                    dedicated_section = bool(suffix) and (
                        urlsplit(old["url"]).path.rstrip("/") == suffix
                        or suffix + "/" in urlsplit(old["url"]).path
                    )
                    if (
                        old["url"].rstrip("/").endswith("/rss/g1" + suffix)
                        or item.get("reason")
                        and dedicated_section
                    ) and not existing["label"]:
                        before = dict(existing)
                        db.execute(
                            "UPDATE links SET label=?,version=version+1,updated_at=? WHERE id=?",
                            (source["name"], now, existing["id"]),
                        )
                        after = dict(
                            db.execute(
                                "SELECT * FROM links WHERE id=?", (existing["id"],)
                            ).fetchone()
                        )
                        store._event(db, "endpoint", existing["id"], "update", before, after)
                        counts["labeled_links"] += 1
                    continue
                # Nova associação conserva o XML histórico. Teste da outra fonte fica na origem.
                cursor = db.execute(
                    "INSERT INTO links(source_id,url,kind,label,notes,active,created_at,"
                    "updated_at,evidence,origin,model,last_test) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'{}')",
                    (
                        target["id"],
                        old["url"],
                        old["kind"],
                        old["label"],
                        old["notes"],
                        int(bool(old["active"] and source["active"])),
                        now,
                        now,
                        old["evidence"],
                        f"consolidated:{source['id']}:{old['id']}",
                        old["model"],
                    ),
                )
                after = dict(
                    db.execute("SELECT * FROM links WHERE id=?", (cursor.lastrowid,)).fetchone()
                )
                store._event(db, "endpoint", cursor.lastrowid, "consolidate", old, after)
                counts["added_links"] += 1
            db.execute(
                "UPDATE sites SET deleted_at=?,active=0,updated_at=?,version=version+1 WHERE id=?",
                (now, now, source["id"]),
            )
            after = dict(db.execute("SELECT * FROM sites WHERE id=?", (source["id"],)).fetchone())
            after["consolidated_into"] = target["id"]
            store._event(db, "source", source["id"], "consolidate", source, after)
            counts["sources"] += 1
        if plan:
            db.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                (metadata_key, json.dumps(dict(at=_stamp(), counts=counts, plan=plan))),
            )
    return counts
