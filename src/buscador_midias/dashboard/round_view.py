"""Consulta isolada da rodada 12, sem custos históricos ou etapas de enriquecimento."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from buscador_midias.analysis.headline import headline_text
from buscador_midias.dashboard.snapshot import _live_progress
from buscador_midias.discovery.url_filter import l0_filter
from buscador_midias.domain.site import Site
from buscador_midias.state.store import StateStore

RUN_ID = 12


def window(directory: Path) -> tuple[datetime, datetime]:
    """A janela fixa da execução é lida da configuração, nunca inferida da data atual."""
    data = json.loads((directory / "configuracao.json").read_text(encoding="utf-8"))
    until = datetime.fromisoformat(data["window_reference_at"])
    if until.tzinfo is None:
        raise ValueError("janela da rodada sem fuso horário")
    return until - timedelta(days=2), until


def round_summary(
    store: StateStore,
    sites: dict[str, Site],
    directory: Path,
    now: datetime,
) -> dict[str, Any] | None:
    """Reúne todas as fontes, com contadores exclusivos desta rodada e da sua janela."""
    run = next((r for r in store.list_runs() if r.id == RUN_ID), None)
    if run is None:
        return None
    since, until = window(directory)
    records = {r.site_key: r for r in store.site_runs(RUN_ID)}
    progress = _live_progress(directory / "execucao.json", RUN_ID, now)
    state = "completed" if run.finished_at else "running" if progress else "stale"
    closure_path = directory / "encerramento.json"
    closure = json.loads(closure_path.read_text(encoding="utf-8")) if closure_path.exists() else {}
    if run.finished_at and closure.get("run_id") == RUN_ID and closure.get("partial"):
        state = "partial"
    rows = []
    for key, site in sites.items():
        record = records.get(key)
        count = (record.new_urls + record.baseline_urls + record.known_urls) if record else 0
        category = (
            "pending"
            if record is None
            else (
                "results"
                if record.status == "ok" and count
                else "empty"
                if record.status == "ok"
                else "problem"
            )
        )
        rows.append(
            {
                "key": key,
                "name": site.name,
                "country": site.country,
                "category": category,
                "links": count,
                "detail": record.detail
                if record
                else (
                    "Varredura parcial; encerrada por solicitação do usuário"
                    if state == "partial"
                    else "Leitura ainda em andamento"
                ),
                "status": record.status if record else "pending",
            }
        )
    return {
        "run_id": RUN_ID,
        "generated_at": now.isoformat(),
        "state": state,
        "window_since": since.isoformat(),
        "window_until": until.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "completed": len(records),
        "total": len(sites),
        "accessible": sum(r.status == "ok" for r in records.values()),
        "links": sum(row["links"] for row in rows),
        "documents_remaining": closure.get("documents_queued", progress.get("documents_queued")),
        "sources": rows,
    }


def round_links(
    store: StateStore,
    directory: Path,
    *,
    site: str = "",
    before: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Página por cursor de URLs datadas da rodada 12; não inclui outras rodadas nem descartes."""
    since, until = window(directory)
    clauses = [
        "first_seen_run = %s",
        "kind IN ('new','baseline','known')",
        "COALESCE(publication_date,lastmod) BETWEEN %s AND %s",
    ]
    params: list[Any] = [RUN_ID, since, until]
    if site:
        clauses.append("site_key = %s")
        params.append(site)
    if before is not None:
        clauses.append("id < %s")
        params.append(before)
    # Busca um pequeno excedente para remover páginas que o histórico da linha de base
    # gravou antes da aplicação do filtro estrutural de notícias.
    limit = min(max(limit, 1), 100)
    batch = limit * 2
    params.append(batch + 1)
    rows = store._all(
        "SELECT id,url,site_key,title,publication_date,lastmod FROM midias_crawler_urls WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT %s",
        params,
    )
    selected = rows[:batch]
    items = []
    consumed = None
    for row in selected:
        consumed = int(row[0])
        if l0_filter(row[1]) is not None:
            continue
        stamp = row[4] or row[5]
        items.append(
            {
                "id": row[0],
                "url": row[1],
                "site": row[2],
                "title": headline_text(row[1], row[3]),
                "date": stamp.isoformat(),
                "date_basis": "publication" if row[4] else "modification",
            }
        )
        if len(items) >= limit:
            break
    more = consumed is not None and (len(rows) > batch or consumed != int(rows[-1][0]))
    return {"run_id": RUN_ID, "items": items, "next_cursor": consumed if more else None}
