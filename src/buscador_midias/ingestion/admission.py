"""Seleção justa por hostname antes de limitar publicações de acesso HTTP."""

from __future__ import annotations

from typing import Any


def network_candidates(
    db: Any, stage: str, recovery: bool, occupied: set[str], limit: int
) -> list[dict[str, Any]]:
    """Alterna hosts, preservando FIFO dentro de cada host e uma tarefa em trânsito.

    A trava transacional do publicador coordena descoberta, fetch e recuperação.
    Hosts ocupados são excluídos antes do ranking/limite; um prefixo com milhares
    de URLs do mesmo host não pode esconder outros sites elegíveis.
    """
    return list(
        db.execute(
            "WITH eligible AS ("
            "SELECT o.id,o.job_id,o.kind,o.stage,o.ready_at,u.url,"
            "rtrim(trim(both '[]' FROM regexp_replace(split_part(lower(u.url),'/',3),"
            " ':[0-9]+$','')),'.') host "
            "FROM ingest_outbox o JOIN ingest_jobs j ON j.id=o.job_id "
            "LEFT JOIN ingest_articles a ON a.hash=j.hash "
            "CROSS JOIN LATERAL (SELECT COALESCE(a.url,j.payload->>'url') url) u "
            "WHERE o.published_at IS NULL AND o.discarded_at IS NULL AND o.ready_at<=now() "
            "AND o.dispatch=j.dispatch AND o.stage=%s AND "
            "((o.kind='work' AND j.state IN ('pending','retry')) OR "
            "(o.kind='failed' AND j.state='failed')) AND "
            "(o.kind='failed' AND NOT %s OR o.kind='work' AND "
            "(j.failures>0 OR j.payload @> '{\"recovery\":true}')=%s)), "
            "ranked AS (SELECT e.*,h.last_admitted,row_number() OVER "
            "(PARTITION BY CASE WHEN e.kind='failed' THEN e.id::text ELSE e.host END "
            "ORDER BY e.ready_at,e.id) position "
            "FROM eligible e LEFT JOIN ingest_host_admission h ON h.host=e.host "
            "WHERE e.kind='failed' OR (NOT (e.host=ANY(%s::text[])) AND NOT EXISTS "
            "(SELECT 1 FROM ingest_host_cooldowns c WHERE c.host=e.host "
            "AND c.available_at>now()))) "
            "SELECT r.id,r.job_id,r.kind,r.stage,r.url FROM ranked r "
            "JOIN ingest_outbox o ON o.id=r.id WHERE r.position=1 "
            "ORDER BY COALESCE(r.last_admitted,'-infinity'::timestamptz),r.ready_at,r.id "
            "LIMIT %s FOR UPDATE OF o SKIP LOCKED",
            (stage, recovery, recovery, list(occupied), limit),
        ).fetchall()
    )
