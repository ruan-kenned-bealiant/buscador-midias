"""Estatísticas por rodada de busca, para o painel ler números prontos.

Cada rodada vira um JSON em ``bm_runs.stats``: recalculado enquanto a rodada está aberta (o painel
pede e tolera alguns segundos de atraso) e fechado uma vez quando ela termina, pelo agendador
(``finalize_pending``). A notícia é uma linha só, então o funil sai de uma agregação simples:
a etapa em que ela está diz por quais etapas já passou.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from buscador_midias.catalog.store_errors import CatalogError

TOP_FAILURES = 15
ARTICLE_STAGES = ("dedup", "fetch", "context", "evidence", "persist")
_OPEN = ("pending", "queued", "running", "retry")
# Contadores que a descoberta grava em cada leitura e que a auditoria do painel mostra.
_AUDIT = dict(
    sampled="head_audit_sampled",
    would_skip="head_shadow_would_skip",
    missed="head_shadow_missed",
    head_cut="head_cut",
    head_stopped="head_stopped_download",
    not_modified="not_modified_responses",
    contradicted="read_order_contradicted",
    avoided_xml="avoided_xml_requests",
)
_COSTS = (
    "bright_data_requests",
    "prompt_tokens",
    "completion_tokens",
    "avoided_article_requests",
    "avoided_xml_requests",
    "cost_usd",
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _funnel(db: psycopg.Connection[dict[str, Any]], run_id: str) -> dict[str, dict[str, int]]:
    """Contagem por etapa e situação; ``succeeded`` é quem já passou da etapa."""
    stages: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def add(stage: str, state: str, n: int) -> None:
        stages[stage][state] += n

    for row in db.execute(
        "SELECT stage,state,failed_stage,count(*) n FROM bm_articles WHERE run_id=%s "
        "GROUP BY 1,2,3",
        (run_id,),
    ).fetchall():
        stage = (
            row["failed_stage"]
            if row["state"] == "failed" and row["failed_stage"]
            else row["stage"]
        )
        index = ARTICLE_STAGES.index(stage)
        for earlier in ARTICLE_STAGES[:index]:
            add(earlier, "succeeded", row["n"])
        if row["state"] == "done":
            add(stage, "succeeded", row["n"])
        else:
            add(stage, row["state"], row["n"])
    for row in db.execute(
        "SELECT state,count(*) n FROM bm_discovery_tasks WHERE run_id=%s GROUP BY 1", (run_id,)
    ).fetchall():
        add("discovery", "succeeded" if row["state"] == "done" else row["state"], row["n"])
    return {stage: dict(states) for stage, states in stages.items()}


def _costs(db: psycopg.Connection[dict[str, Any]], run_id: str) -> dict[str, float]:
    """Custos somados das notícias da rodada e das leituras de descoberta."""
    sums = ",".join(f"COALESCE(sum((v->>'{key}')::numeric),0) {key}" for key in _COSTS)
    total: dict[str, float] = dict.fromkeys(_COSTS, 0.0)
    for row in (
        db.execute(
            f"SELECT {sums} FROM bm_articles a CROSS JOIN LATERAL jsonb_each(a.cost) e(stage,v) "  # noqa: S608
            "WHERE a.run_id=%s",
            (run_id,),
        ).fetchone(),
        db.execute(
            f"SELECT {sums} FROM bm_discovery_tasks t, LATERAL (SELECT t.result->'metrics' v) e "  # noqa: S608
            "WHERE t.run_id=%s AND jsonb_typeof(t.result->'metrics')='object'",
            (run_id,),
        ).fetchone(),
    ):
        for key in _COSTS:
            total[key] += float((row or {}).get(key) or 0)
    return total


def compute(db: psycopg.Connection[dict[str, Any]], run: dict[str, Any]) -> dict[str, Any]:
    """Resume uma rodada a partir das notícias, tarefas e leituras dela."""
    run_id = run["id"]
    stages = _funnel(db, run_id)
    reads = db.execute(
        "SELECT count(*) links,count(*) FILTER (WHERE discovery_done) done,"
        "COALESCE(sum(listed),0) occurrences FROM bm_run_links WHERE run_id=%s",
        (run_id,),
    ).fetchone()
    unique = db.execute("SELECT count(*) n FROM bm_articles WHERE run_id=%s", (run_id,)).fetchone()
    sums = ",".join(
        f"COALESCE(sum((metrics->>'{key}')::int),0) {name}" for name, key in _AUDIT.items()
    )
    audit = db.execute(
        f"SELECT count(*) discovery_reads,{sums} FROM "  # noqa: S608
        "(SELECT CASE WHEN jsonb_typeof(result->'metrics')='object' THEN result->'metrics' "
        "ELSE '{}'::jsonb END metrics FROM bm_discovery_tasks WHERE run_id=%s) t",
        (run_id,),
    ).fetchone()
    failures = db.execute(
        "SELECT stage,reason,sum(n)::int n FROM ("
        "SELECT failed_stage stage,left(COALESCE(reason,'(sem motivo)'),90) reason,count(*) n "
        "FROM bm_articles WHERE run_id=%(run)s AND state='failed' GROUP BY 1,2 UNION ALL "
        "SELECT 'discovery',left(COALESCE(reason,outcome,'(sem motivo)'),90),count(*) "
        "FROM bm_discovery_tasks WHERE run_id=%(run)s AND state='failed' GROUP BY 1,2) f "
        "GROUP BY 1,2 ORDER BY n DESC,stage,reason LIMIT %(limit)s",
        dict(run=run_id, limit=TOP_FAILURES),
    ).fetchall()
    clock = db.execute("SELECT now() now").fetchone()
    assert clock
    now = clock["now"]
    finished = run["finished_at"]
    costs = _costs(db, run_id)
    assert reads and unique and audit
    return dict(
        run_id=run_id,
        trigger=run["trigger"],
        state=run["status"],
        started_at=_iso(run["created_at"]),
        finished_at=_iso(finished),
        since=_iso(run["since"]),
        until=_iso(run["until"]),
        computed_at=_iso(now),
        duration_s=int(((finished or now) - run["created_at"]).total_seconds()),
        final=finished is not None,
        links=reads["links"],
        links_done=reads["done"],
        adaptive_skipped=int(run["config"].get("adaptive_skipped", 0)),
        head_filter=run["config"].get("head_filter", "shadow"),
        stages=stages,
        persisted=stages.get("persist", {}).get("succeeded", 0),
        occurrences=int(reads["occurrences"]),
        unique_articles=unique["n"],
        cost_usd=costs["cost_usd"],
        tokens_in=int(costs["prompt_tokens"]),
        tokens_out=int(costs["completion_tokens"]),
        bright_requests=int(costs["bright_data_requests"]),
        audit=dict(audit),
        failures=[dict(row) for row in failures],
    )


def refresh(db: psycopg.Connection[dict[str, Any]], run_id: str) -> dict[str, Any]:
    """Recalcula e grava as estatísticas da rodada; devolve o resultado."""
    run = db.execute("SELECT * FROM bm_runs WHERE id=%s AND type='search'", (run_id,)).fetchone()
    if not run:
        raise CatalogError("Rodada não encontrada.", 404)
    stats = compute(db, run)
    db.execute(
        "UPDATE bm_runs SET stats=%s,stats_final=%s WHERE id=%s",
        (Jsonb(stats), stats["final"], run_id),
    )
    return stats


def finalize_pending(db: psycopg.Connection[dict[str, Any]], limit: int = 1) -> list[str]:
    """Fecha as estatísticas das rodadas já terminadas que ainda não as têm (mais novas antes)."""
    pending = [
        row["id"]
        for row in db.execute(
            "SELECT id FROM bm_runs WHERE type='search' AND finished_at IS NOT NULL "
            "AND NOT stats_final ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    ]
    for run_id in pending:
        refresh(db, run_id)
    return pending


def load(db: psycopg.Connection[dict[str, Any]], run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Estatísticas já gravadas das rodadas pedidas (as que não existem ficam de fora)."""
    rows = db.execute(
        "SELECT id,stats FROM bm_runs WHERE id=ANY(%s) AND stats<>'{}'::jsonb", (run_ids,)
    ).fetchall()
    return {row["id"]: row["stats"] for row in rows}
