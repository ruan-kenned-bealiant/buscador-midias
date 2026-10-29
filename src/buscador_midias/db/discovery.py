"""Concluir a descoberta de um documento: tarefas filhas, continuação, URLs e fim da rodada."""

from __future__ import annotations

import gzip
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.queue import DISCOVERY, admit, entry, host_of, locked

OPEN_STATES = "('pending','queued','running','retry')"
_BULKY = ("candidates", "children", "article")


def save_plan(db: psycopg.Connection[Any], job: dict[str, Any], plan: dict[str, Any]) -> str:
    """Guarda o documento já interpretado na própria tarefa; continuações apontam para ela."""
    body = gzip.compress(json.dumps(plan, ensure_ascii=False).encode(), compresslevel=4)
    db.execute("UPDATE bm_discovery_tasks SET plan=%s WHERE id=%s", (body, job["id"]))
    return str(job["id"])


def read_plan(db: psycopg.Connection[Any], key: str) -> dict[str, Any]:
    """Recupera o plano sem repetir HTTP nem a expansão recursiva do documento."""
    row = db.execute("SELECT plan FROM bm_discovery_tasks WHERE id=%s", (int(key),)).fetchone()
    if not row or row["plan"] is None:
        raise CatalogError("Plano de documento não encontrado.")
    return dict(json.loads(gzip.decompress(row["plan"])))


def _enqueue(
    db: psycopg.Connection[Any],
    job: dict[str, Any],
    url: str,
    depth: int,
    slice_: int = 0,
    plan_ref: int | None = None,
) -> None:
    db.execute(
        "INSERT INTO bm_discovery_tasks(run_id,link_id,url,depth,slice,plan_ref,host) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id,link_id,url,slice) DO NOTHING",
        (job["run_id"], job["link_id"], url, depth, slice_, plan_ref, host_of(url)),
    )


def complete_discovery(
    db: psycopg.Connection[Any], job: dict[str, Any], output: dict[str, Any]
) -> bool:
    """Confirma a leitura de um documento: filhos, continuação e URLs saem na mesma transação.

    Devolve ``False`` se a reserva foi substituída por outro consumidor.
    """
    row = locked(db, job, DISCOVERY)
    if row is None:
        return False
    continuation, plan_key = output.get("continuation"), output.get("plan_key")
    if continuation and plan_key is None:
        raise ValueError("Uma continuação precisa do plano que ela continua.")
    for child in dict.fromkeys(output.get("children", [])):
        _enqueue(db, job, child, job["depth"] + 1)
    if continuation and plan_key is not None:
        _enqueue(db, job, job["url"], job["depth"], continuation["cursor"], int(plan_key))
    elif plan_key is not None:
        db.execute("UPDATE bm_discovery_tasks SET plan=NULL WHERE id=%s", (int(plan_key),))
    candidates = output.get("candidates", [])
    if candidates:
        read = db.execute(
            "SELECT site_id FROM bm_run_links WHERE run_id=%s AND link_id=%s",
            (job["run_id"], job["link_id"]),
        ).fetchone()
        assert read is not None
        admit(
            db,
            candidates,
            run=job["run"],
            link=dict(id=job["link_id"], site_id=read["site_id"]),
            document_url=job["url"],
        )
    summary = {k: v for k, v in output.items() if k not in _BULKY}
    summary.update(candidates=len(candidates), children=len(output.get("children", [])))
    history = [*row["history"], entry("discovery", job["attempts"], "ok")][-10:]
    db.execute(
        "UPDATE bm_discovery_tasks SET state='done',outcome=%s,result=%s,history=%s,"
        "owner=NULL,locked_until=NULL,queued_at=NULL,finished_at=now(),updated_at=now() "
        "WHERE id=%s",
        (output.get("outcome", "ok"), Jsonb(summary), Jsonb(history), job["id"]),
    )
    db.execute(
        "UPDATE bm_run_links SET state='running' WHERE run_id=%s AND link_id=%s "
        "AND state='pending'",
        (job["run_id"], job["link_id"]),
    )
    close_reads(db, job["run_id"])
    return True


def close_reads(db: psycopg.Connection[Any], run_id: str | None = None) -> int:
    """Encerra a leitura dos links cujas tarefas de descoberta já terminaram (ou falharam)."""
    return db.execute(
        "UPDATE bm_run_links rl SET discovery_done=true,finished_at=now(),"
        "state=CASE WHEN EXISTS (SELECT 1 FROM bm_discovery_tasks t WHERE t.run_id=rl.run_id "
        "AND t.link_id=rl.link_id AND t.state IN ('failed','cancelled')) "
        "THEN 'error' ELSE 'completed' END "
        "WHERE rl.discovery_done=false AND (%(run)s::text IS NULL OR rl.run_id=%(run)s) "
        "AND NOT EXISTS (SELECT 1 FROM bm_discovery_tasks t WHERE t.run_id=rl.run_id "
        f"AND t.link_id=rl.link_id AND t.state IN {OPEN_STATES})",
        dict(run=run_id),
    ).rowcount


def finish_runs(db: psycopg.Connection[Any]) -> int:
    """Fecha as rodadas de busca sem leitura nem notícia em aberto.

    Termina ``partial`` se alguma leitura ou notícia falhou, foi cancelada, ficou sem data ou
    sem evidência; ``completed`` caso contrário. Cada notícia pertence à rodada que a admitiu
    por último (``run_id``), então uma rodada nunca espera por trabalho de outra.
    """
    return db.execute(
        "UPDATE bm_runs r SET finished_at=now(),status=CASE WHEN "
        "EXISTS (SELECT 1 FROM bm_run_links rl WHERE rl.run_id=r.id AND rl.state='error') "
        "OR EXISTS (SELECT 1 FROM bm_discovery_tasks t WHERE t.run_id=r.id "
        "AND t.state IN ('failed','cancelled')) "
        "OR EXISTS (SELECT 1 FROM bm_articles a WHERE a.run_id=r.id AND (a.state IN "
        "('failed','cancelled') OR a.outcome IN ('date_unknown','evidence_pending'))) "
        "THEN 'partial' ELSE 'completed' END "
        "WHERE r.type='search' AND r.status='running' "
        "AND NOT EXISTS (SELECT 1 FROM bm_run_links rl WHERE rl.run_id=r.id "
        "AND rl.discovery_done IS NOT TRUE) "
        f"AND NOT EXISTS (SELECT 1 FROM bm_discovery_tasks t WHERE t.run_id=r.id "
        f"AND t.state IN {OPEN_STATES}) "
        f"AND NOT EXISTS (SELECT 1 FROM bm_articles a WHERE a.run_id=r.id "
        f"AND a.state IN {OPEN_STATES})"
    ).rowcount
