"""Seleção justa por hostname antes de limitar publicações de acesso HTTP."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Hosts distintos com trabalho pronto, lidos por salto no índice (stage, host): o custo cresce
# com o número de hosts, não com o tamanho da fila. A primeira tarefa de cada host respeita a
# ordem de chegada; a coluna ``host`` do outbox é preenchida por gatilho na inserção.
_WORK = (
    "WITH RECURSIVE hosts(host) AS ("
    "(SELECT host FROM ingest_outbox WHERE stage=%(stage)s AND published_at IS NULL "
    "AND discarded_at IS NULL AND host IS NOT NULL ORDER BY host LIMIT 1) "
    "UNION ALL "
    "SELECT (SELECT o.host FROM ingest_outbox o WHERE o.stage=%(stage)s "
    "AND o.published_at IS NULL AND o.discarded_at IS NULL AND o.host>h.host "
    "ORDER BY o.host LIMIT 1) FROM hosts h WHERE h.host IS NOT NULL), "
    "ranked AS (SELECT x.id,x.job_id,x.kind,x.stage,x.url,x.ready_at,a.last_admitted "
    "FROM hosts h LEFT JOIN ingest_host_admission a ON a.host=h.host "
    "CROSS JOIN LATERAL (SELECT o.id,o.job_id,o.kind,o.stage,o.ready_at,"
    "COALESCE(art.url,j.payload->>'url') url FROM ingest_outbox o "
    "JOIN ingest_jobs j ON j.id=o.job_id LEFT JOIN ingest_articles art ON art.hash=j.hash "
    "WHERE o.stage=%(stage)s AND o.host=h.host AND o.published_at IS NULL "
    "AND o.discarded_at IS NULL AND o.ready_at<=now() AND o.dispatch=j.dispatch "
    "AND o.kind='work' AND j.state IN ('pending','retry') "
    "AND (j.failures>0 OR j.payload @> '{\"recovery\":true}')=%(recovery)s "
    "ORDER BY o.ready_at,o.id LIMIT %(per_host)s) x "
    "WHERE h.host IS NOT NULL AND NOT (h.host=ANY(%(full)s::text[])) AND NOT EXISTS "
    "(SELECT 1 FROM ingest_host_cooldowns c WHERE c.host=h.host AND c.available_at>now())) "
    "SELECT r.id,r.job_id,r.kind,r.stage,r.url FROM ranked r "
    "JOIN ingest_outbox o ON o.id=r.id "
    "ORDER BY COALESCE(r.last_admitted,'-infinity'::timestamptz),r.ready_at,r.id "
    "LIMIT %(limit)s FOR UPDATE OF o SKIP LOCKED"
)

# Hosts com trabalho novo (nunca tentado, fora da recuperação) pronto agora. Salta de host em
# host pelo índice (stage, host) e sonda cada um com EXISTS, em vez de listar todas as linhas:
# o custo cresce com o número de hosts, não com o tamanho do backlog.
_PRIMARY = (
    "WITH RECURSIVE hosts(host) AS ("
    "(SELECT host FROM ingest_outbox WHERE stage=%(stage)s AND published_at IS NULL "
    "AND discarded_at IS NULL AND host IS NOT NULL ORDER BY host LIMIT 1) "
    "UNION ALL "
    "SELECT (SELECT o.host FROM ingest_outbox o WHERE o.stage=%(stage)s "
    "AND o.published_at IS NULL AND o.discarded_at IS NULL AND o.host>h.host "
    "ORDER BY o.host LIMIT 1) FROM hosts h WHERE h.host IS NOT NULL) "
    "SELECT h.host FROM hosts h WHERE h.host IS NOT NULL AND EXISTS ("
    "SELECT 1 FROM ingest_outbox o JOIN ingest_jobs j ON j.id=o.job_id "
    "WHERE o.stage=%(stage)s AND o.host=h.host AND o.published_at IS NULL "
    "AND o.discarded_at IS NULL AND o.ready_at<=now() AND o.dispatch=j.dispatch "
    "AND o.kind='work' AND j.state IN ('pending','retry') AND j.failures=0 "
    "AND NOT j.payload @> '{\"recovery\":true}')"
)

_FAILED = (
    "SELECT o.id,o.job_id,o.kind,o.stage,COALESCE(a.url,j.payload->>'url') url "
    "FROM ingest_outbox o JOIN ingest_jobs j ON j.id=o.job_id "
    "LEFT JOIN ingest_articles a ON a.hash=j.hash "
    "WHERE o.stage=%(stage)s AND o.kind='failed' AND j.state='failed' "
    "AND o.published_at IS NULL AND o.discarded_at IS NULL AND o.ready_at<=now() "
    "AND o.dispatch=j.dispatch ORDER BY o.ready_at,o.id LIMIT %(limit)s "
    "FOR UPDATE OF o SKIP LOCKED"
)


def primary_hosts(db: Any) -> set[str]:
    """Hosts de descoberta ou fetch com trabalho novo pronto, que a recuperação deve respeitar."""
    return {
        row["host"]
        for stage in ("discovery", "fetch")
        for row in db.execute(_PRIMARY, dict(stage=stage)).fetchall()
    }


def network_candidates(
    db: Any,
    stage: str,
    recovery: bool,
    occupied: Mapping[str, int],
    limit: int,
    per_host: int = 1,
) -> list[dict[str, Any]]:
    """Alterna hosts, preservando FIFO dentro de cada host e ``per_host`` tarefas em trânsito.

    A trava transacional do publicador coordena descoberta, fetch e recuperação.
    ``occupied`` conta as tarefas já em trânsito por host; hosts que atingiram ``per_host``
    são excluídos antes do ranking/limite, e um prefixo com milhares de URLs do mesmo host
    não esconde outros sites elegíveis. Mensagens de falha (dead-letter) só seguem fora da
    recuperação e não dependem do host.
    """
    rows: list[dict[str, Any]] = []
    if not recovery:
        rows += db.execute(_FAILED, dict(stage=stage, limit=limit)).fetchall()
    if len(rows) < limit:
        rows += db.execute(
            _WORK,
            dict(
                stage=stage,
                recovery=recovery,
                per_host=per_host,
                full=[host for host, count in occupied.items() if count >= per_host],
                limit=limit - len(rows),
            ),
        ).fetchall()
    return rows


def confirm_published(db: Any, outbox_ids: list[int]) -> None:
    """Marca o lote como publicado e as tarefas como enfileiradas, na mesma transação.

    As linhas de ``ingest_jobs`` são travadas em ordem de ``id``, a mesma de ``claim_many``;
    sem ordem fixa, publicador e workers se travavam mutuamente (deadlock). O worker pode ter
    finalizado antes; não regredir estado/despacho.
    """
    db.execute(
        "UPDATE ingest_outbox o SET published_at=now() FROM (SELECT id FROM ingest_outbox "
        "WHERE id=ANY(%s) ORDER BY id FOR UPDATE) l WHERE o.id=l.id",
        (outbox_ids,),
    )
    db.execute(
        "WITH locked AS (SELECT j.id FROM ingest_jobs j JOIN ingest_outbox o ON j.id=o.job_id "
        "WHERE o.id=ANY(%s) AND j.dispatch=o.dispatch AND j.state IN ('pending','retry') "
        "ORDER BY j.id FOR UPDATE OF j) "
        "UPDATE ingest_jobs j SET state='queued',updated_at=now() FROM locked l WHERE j.id=l.id",
        (outbox_ids,),
    )
