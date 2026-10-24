"""Progresso por fonte e rodada, sem contar uma notícia duas vezes pelos seus links."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion.schema import STAGES

ACTIVE = {"pending", "fetch", "context", "evidence", "persist"}
STEPS = dict(pending=0, fetch=1, context=2, evidence=3, persist=4)


def summarize(
    source: dict[str, Any], statuses: dict[str, int], queues: list[Any]
) -> dict[str, Any]:
    """Exibe conclusão com pendências como 100% encerrado, sem confundir com sucesso."""
    links = int(source["links"])
    links_done = int(source["links_done"])
    articles = sum(statuses.values())
    total = links + articles * 5
    done = links_done + sum(STEPS.get(status, 5) * n for status, n in statuses.items())
    in_discovery = links_done < links
    waiting = sum(n for status, n in statuses.items() if status in ACTIVE)
    open_jobs = sum(
        int(q["n"]) for q in queues if q["state"] in ("pending", "queued", "running", "retry")
    )
    errors = max(
        statuses.get("failed", 0), sum(int(q["n"]) for q in queues if q["state"] == "failed")
    )
    closed = not in_discovery and waiting == 0 and open_jobs == 0
    progress = min(99, int(done * 100 / total)) if total and not closed else 100 if closed else 0
    return dict(
        source,
        articles=articles,
        statuses=statuses,
        stages=queues,
        errors=errors,
        active_articles=waiting,
        discovery_complete=not in_discovery,
        progress=None if in_discovery else progress,
        state="partial" if closed and errors else "completed" if closed else "running",
    )


def source_progress(
    repository: Any, run_id: str = "", offset: int = 0, search: str = ""
) -> dict[str, Any]:
    """Agrega uma rodada: fases compartilhadas aparecem para todas as fontes de origem."""
    if (run_id and not re.fullmatch(r"[a-f0-9]{32}", run_id)) or not 0 <= offset <= 1000000:
        raise CatalogError("Rodada ou paginação inválida.")
    repository.initialize()
    with repository.connection() as db:
        run = db.execute(
            "SELECT * FROM ingest_runs WHERE %s='' OR id=%s ORDER BY created_at DESC LIMIT 1",
            (run_id, run_id),
        ).fetchone()
        if not run:
            if run_id:
                raise CatalogError("Rodada não encontrada.", 404)
            return dict(run=None, sources=[], total=0, offset=offset, summary={})
        ident = run["id"]
        sources = db.execute(
            "SELECT (snapshot->'source'->>'id')::bigint id,"
            "max(snapshot->'source'->>'name') name,max(snapshot->'source'->>'url') url,"
            "count(*) links,count(*) FILTER(WHERE discovery_done) links_done "
            "FROM ingest_polls WHERE run_id=%s GROUP BY (snapshot->'source'->>'id')::bigint",
            (ident,),
        ).fetchall()
        # Resultado da última geração desta rodada conserva o histórico mesmo se outra
        # rodada reabrir a notícia. Sem job local, acompanha o trabalho já compartilhado.
        statuses = db.execute(
            "WITH origins AS (SELECT DISTINCT (p.snapshot->'source'->>'id')::bigint source_id,"
            "o.hash FROM ingest_occurrences o JOIN ingest_polls p ON p.id=o.poll_id "
            "WHERE o.run_id=%s), latest AS (SELECT DISTINCT ON(hash) hash,stage,state,result "
            "FROM ingest_jobs WHERE run_id=%s AND hash IS NOT NULL ORDER BY hash,generation "
            "DESC,CASE stage WHEN 'persist' THEN 6 WHEN 'evidence' THEN 5 WHEN 'context' THEN 4 "
            "WHEN 'fetch' THEN 3 ELSE 2 END DESC,created_at DESC) "
            "SELECT o.source_id,CASE WHEN j.state IN ('failed','cancelled','skipped') THEN j.state "
            "WHEN j.state='succeeded' THEN "
            "COALESCE(j.result->>'status',j.result->>'next',a.status) "
            "WHEN j.stage='dedup' THEN 'pending' ELSE COALESCE(j.stage,a.status) END status,"
            "count(*) n FROM origins o JOIN ingest_articles a ON a.hash=o.hash "
            "LEFT JOIN latest j ON j.hash=o.hash GROUP BY 1,2",
            (ident, ident),
        ).fetchall()
        queues = db.execute(
            "WITH origins AS (SELECT DISTINCT (p.snapshot->'source'->>'id')::bigint source_id,"
            "o.hash FROM ingest_occurrences o JOIN ingest_polls p ON p.id=o.poll_id "
            "WHERE o.run_id=%s) SELECT COALESCE(o.source_id,"
            "(p.snapshot->'source'->>'id')::bigint) source_id,j.stage,j.state,count(*) n "
            "FROM ingest_jobs j JOIN ingest_polls p ON p.id=j.poll_id "
            "LEFT JOIN origins o ON o.hash=j.hash WHERE j.run_id=%s "
            "AND (j.hash IS NULL OR j.generation=(SELECT max(g.generation) FROM ingest_jobs g "
            "WHERE g.run_id=j.run_id AND g.hash=j.hash)) "
            "GROUP BY 1,2,3 ORDER BY j.stage,j.state",
            (ident, ident),
        ).fetchall()
    by_source: dict[int, dict[str, int]] = defaultdict(dict)
    stage_rows: dict[int, list[Any]] = defaultdict(list)
    for row in statuses:
        by_source[row["source_id"]][row["status"]] = int(row["n"])
    for row in queues:
        stage_rows[row["source_id"]].append(
            dict(stage=row["stage"], state=row["state"], n=row["n"])
        )
    rows = [summarize(dict(s), by_source[s["id"]], stage_rows[s["id"]]) for s in sources]
    summary = dict(Counter(row["state"] for row in rows))
    filtered = sorted(
        (r for r in rows if search.casefold() in r["name"].casefold()),
        key=lambda r: (r["state"] == "completed", r["name"].casefold()),
    )
    return dict(
        run=run,
        sources=filtered[offset : offset + 25],
        total=len(filtered),
        offset=offset,
        summary=summary,
        stage_names=list(STAGES),
    )
