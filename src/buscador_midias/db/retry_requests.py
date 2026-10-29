"""Pedidos de reprocessamento: o que uma pessoa mandou reabrir e como isso terminou."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

RECENT = 10
REASONS_SHOWN = 3
_OPEN = "('pending','queued','running','retry')"


def stored_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """O filtro como foi pedido, sem as listas de notícias (guarda só quantas eram)."""
    return {
        key: len(value) if key == "hashes" else value
        for key, value in filters.items()
        if key != "everything" and value
    } | ({"everything": True} if filters.get("everything") is True else {})


def open_request(db: psycopg.Connection[Any], filters: dict[str, Any]) -> str:
    """Registra o pedido antes de reabrir as notícias, para que elas apontem para ele."""
    request_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO bm_retry_requests(id,filters) VALUES(%s,%s)",
        (request_id, Jsonb(stored_filters(filters))),
    )
    return request_id


def close_request(db: psycopg.Connection[Any], request_id: str, requested: int) -> str | None:
    """Fixa quantas notícias o pedido reabriu; um pedido que não reabriu nada não fica."""
    if requested == 0:
        db.execute("DELETE FROM bm_retry_requests WHERE id=%s", (request_id,))
        return None
    db.execute("UPDATE bm_retry_requests SET requested=%s WHERE id=%s", (requested, request_id))
    return request_id


def recent(db: psycopg.Connection[dict[str, Any]], limit: int = RECENT) -> list[dict[str, Any]]:
    """Os pedidos mais novos, cada um com o andamento das notícias que reabriu."""
    rows = db.execute(
        "SELECT r.id,r.created_at,r.filters,r.requested,"
        f"count(a.hash) FILTER (WHERE a.state IN {_OPEN}) in_progress,"  # noqa: S608
        "count(a.hash) FILTER (WHERE a.state='done' AND a.outcome='persisted') persisted,"
        "count(a.hash) FILTER (WHERE a.state='done' AND a.outcome IS DISTINCT FROM 'persisted') "
        "done_other,"
        "count(a.hash) FILTER (WHERE a.state='failed') failed,"
        "count(a.hash) FILTER (WHERE a.state='cancelled') cancelled,"
        "r.requested-count(a.hash) superseded "
        "FROM bm_retry_requests r LEFT JOIN bm_articles a ON a.retry_id=r.id "
        "GROUP BY r.id ORDER BY r.created_at DESC,r.id LIMIT %s",
        (limit,),
    ).fetchall()
    ids = [row["id"] for row in rows]
    reasons: dict[str, list[dict[str, Any]]] = {}
    for item in db.execute(
        "SELECT retry_id,coalesce(reason,'') reason,count(*) n FROM bm_articles "
        "WHERE retry_id=ANY(%s) AND state='failed' GROUP BY 1,2 ORDER BY n DESC,2",
        (ids,),
    ).fetchall():
        reasons.setdefault(item["retry_id"], []).append(dict(reason=item["reason"], n=item["n"]))
    names = _site_names(db, rows)
    return [
        dict(
            row,
            finished=row["in_progress"] == 0,
            reasons=reasons.get(row["id"], [])[:REASONS_SHOWN],
            source_name=names.get(row["filters"].get("source_id")),
        )
        for row in rows
    ]


def _site_names(
    db: psycopg.Connection[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[Any, str]:
    """Nome dos sites citados nos filtros, para a tela não mostrar só um número."""
    wanted = [row["filters"]["source_id"] for row in rows if row["filters"].get("source_id")]
    if not wanted:
        return {}
    return {
        row["id"]: row["name"]
        for row in db.execute(
            "SELECT id,name FROM midias_ia_trusted_sources WHERE id=ANY(%s)", (wanted,)
        ).fetchall()
    }
