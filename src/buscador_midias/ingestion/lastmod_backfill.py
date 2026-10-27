"""Filtro por ``lastmod`` aplicado às tarefas já criadas de uma rodada.

A descoberta passa a descartar URLs cujo ``lastmod`` é anterior à janela, mas as rodadas
em andamento já têm dezenas de milhares de tarefas criadas sem essa informação. Este módulo
reaproveita os documentos interpretados no cache e a mesma regra de ``lastmod_outside`` para
encerrar, sem baixar nada, as tarefas de dedup/fetch cuja página é comprovadamente anterior.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from typing import Any

from buscador_midias.ingestion.flow import lastmod_outside

OWNER = "lastmod-backfill"
OPEN_STATES = ("pending", "queued", "retry")


def load_hints(db: Any, hours: int) -> dict[str, str]:
    """Une os ``lastmod`` por URL dos documentos lidos nas últimas ``hours`` horas."""
    hints: dict[str, str] = {}
    cursor = db.cursor(name="lastmod_hints")
    cursor.execute(
        "SELECT document FROM ingest_document_cache "
        "WHERE checked_at>now()-make_interval(hours=>%s)",
        (hours,),
    )
    for row in cursor:
        document = json.loads(gzip.decompress(bytes(row["document"])))
        hints.update(document.get("hints") or {})
    cursor.close()
    return hints


def open_tasks(db: Any, run_id: str) -> list[dict[str, Any]]:
    """Tarefas abertas de dedup/fetch; artigo já no corpus só repara evidência e fica de fora."""
    return list(
        db.execute(
            "SELECT j.id,j.stage,COALESCE(a.url,j.payload->>'url') url,"
            "a.payload->'shadow_lastmod_exclusion' flagged FROM ingest_jobs j "
            "JOIN ingest_articles a ON a.hash=j.hash WHERE j.run_id=%s "
            "AND j.stage IN ('dedup','fetch') AND j.state=ANY(%s) "
            "AND NOT a.payload ? 'existing_hash' ORDER BY j.id",
            (run_id, list(OPEN_STATES)),
        ).fetchall()
    )


def select_outside(
    tasks: list[dict[str, Any]], hints: dict[str, str], since: datetime
) -> dict[str, dict[str, Any]]:
    """Decide por tarefa: marca da descoberta em shadow ou ``lastmod`` conhecido no cache."""
    decisions: dict[str, dict[str, Any]] = {}
    for task in tasks:
        found = task["flagged"] or lastmod_outside(hints.get(task["url"]), since)
        if found:
            decisions[task["id"]] = found
    return decisions


def violations(
    db: Any, run_id: str, hints: dict[str, str], since: datetime
) -> list[dict[str, Any]]:
    """Notícias já confirmadas dentro da janela que a regra teria excluído: deve ser zero."""
    found = []
    for row in db.execute(
        "SELECT COALESCE(a.url,j.payload->>'url') url,a.payload->>'published_at' published "
        "FROM ingest_jobs j JOIN ingest_articles a ON a.hash=j.hash WHERE j.run_id=%s "
        "AND j.stage='fetch' AND j.outcome='texto_confirmado'",
        (run_id,),
    ).fetchall():
        rule = lastmod_outside(hints.get(row["url"]), since)
        if rule:
            found.append(dict(url=row["url"], published_at=row["published"], rule=rule))
    return found


def outcome(found: dict[str, Any]) -> dict[str, Any]:
    """Resultado equivalente ao de uma publicação fora da janela, sem requisição ao site."""
    return dict(
        outcome="fora_da_janela_por_lastmod",
        status="outside_window",
        article=dict(date_basis="lastmod_upper_bound", exclusion=found),
        metrics=dict(avoided_article_requests=1),
    )


def backfill(
    repository: Any, run_id: str, hints: dict[str, str], *, apply: bool = False, batch: int = 500
) -> dict[str, Any]:
    """Relatório sempre; encerra as tarefas só com ``apply`` e sem nenhuma violação."""
    with repository.connection() as db:
        since = db.execute("SELECT since FROM ingest_runs WHERE id=%s", (run_id,)).fetchone()[
            "since"
        ]
        tasks = open_tasks(db, run_id)
        decisions = select_outside(tasks, hints, since)
        broken = violations(db, run_id, hints, since)
    stages: dict[str, int] = {}
    for task in tasks:
        if task["id"] in decisions:
            stages[task["stage"]] = stages.get(task["stage"], 0) + 1
    report: dict[str, Any] = dict(
        run_id=run_id,
        open_tasks=len(tasks),
        excludable=len(decisions),
        by_stage=stages,
        violations=len(broken),
        violation_samples=broken[:5],
        applied=0,
    )
    if not apply or broken:
        return report
    ids = list(decisions)
    for start in range(0, len(ids), batch):
        jobs = repository.claim_many(ids[start : start + batch], OWNER)
        repository.finish_many(jobs, [outcome(decisions[job["id"]]) for job in jobs])
        report["applied"] += len(jobs)
    return report
