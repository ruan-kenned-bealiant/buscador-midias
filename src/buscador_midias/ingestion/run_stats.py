"""Estatísticas pré-agregadas por rodada, para o painel ler números prontos.

A tabela de tarefas tem milhões de linhas; somar tudo a cada abertura da tela custava segundos.
Cada rodada vira uma linha em ``ingest_run_stats`` (upsert): recalculada enquanto aberta e
fechada uma vez quando termina, pelo laço do agendador (``finalize_pending``).
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from buscador_midias.catalog.store import CatalogError

TOP_FAILURES = 15
# Contadores que a descoberta grava em cada tentativa e que o painel de auditoria mostra.
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


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _number(value: Any) -> float:
    return float(value) if isinstance(value, Decimal) else float(value or 0)


def compute_run_stats(repository: Any, db: Any, run: dict[str, Any]) -> dict[str, Any]:
    """Resume uma rodada a partir das tarefas, tentativas e ocorrências dela (a parte cara)."""
    run_id = run["id"]
    stages: dict[str, dict[str, int]] = defaultdict(dict)
    for row in db.execute(
        "SELECT stage,state,count(*) n FROM ingest_jobs WHERE run_id=%s GROUP BY 1,2", (run_id,)
    ).fetchall():
        stages[row["stage"]][row["state"]] = row["n"]
    polls = db.execute(
        "SELECT count(*) links,count(*) FILTER (WHERE discovery_done) done "
        "FROM ingest_polls WHERE run_id=%s",
        (run_id,),
    ).fetchone()
    seen = db.execute(
        "SELECT count(*) occurrences,count(DISTINCT hash) unique_articles "
        "FROM ingest_occurrences WHERE run_id=%s",
        (run_id,),
    ).fetchone()
    paid = repository._metrics(db, run_id, 0)
    sums = ",".join(
        f"COALESCE(sum((a.metrics->>'{key}')::int),0) {name}" for name, key in _AUDIT.items()
    )
    audit = dict(
        db.execute(
            f"SELECT count(*) discovery_reads,{sums} FROM ingest_attempts a "  # noqa: S608
            "JOIN ingest_jobs j ON j.id=a.job_id WHERE j.run_id=%s AND j.stage='discovery'",
            (run_id,),
        ).fetchone()
    )
    failures = [
        dict(stage=row["stage"], reason=row["reason"], n=row["n"])
        for row in db.execute(
            "SELECT stage,left(COALESCE(outcome,'(sem motivo)'),90) reason,count(*) n "
            "FROM ingest_jobs WHERE run_id=%s AND state='failed' GROUP BY 1,2 "
            "ORDER BY n DESC,stage,reason LIMIT %s",
            (run_id, TOP_FAILURES),
        ).fetchall()
    ]
    now = db.execute("SELECT now() now").fetchone()["now"]
    finished = run["finished_at"]
    return dict(
        run_id=run_id,
        trigger=run["trigger"],
        state=run["state"],
        started_at=_iso(run["created_at"]),
        finished_at=_iso(finished),
        since=_iso(run["since"]),
        until=_iso(run["until"]),
        computed_at=_iso(now),
        duration_s=int(((finished or now) - run["created_at"]).total_seconds()),
        final=finished is not None,
        links=polls["links"],
        links_done=polls["done"],
        adaptive_skipped=int(run["config"].get("adaptive_skipped", 0)),
        head_filter=run["config"].get("head_filter", "shadow"),
        stages=dict(stages),
        persisted=stages.get("persist", {}).get("succeeded", 0),
        occurrences=seen["occurrences"],
        unique_articles=seen["unique_articles"],
        cost_usd=sum(_number(row["cost_usd"]) for row in paid),
        tokens_in=int(sum(_number(row["prompt_tokens"]) for row in paid)),
        tokens_out=int(sum(_number(row["completion_tokens"]) for row in paid)),
        bright_requests=int(sum(_number(row["bright_data_requests"]) for row in paid)),
        audit=audit,
        failures=failures,
    )


def refresh_run_stats(repository: Any, run_id: str) -> dict[str, Any]:
    """Recalcula e grava (upsert) as estatísticas da rodada; devolve o resultado."""
    repository.initialize()
    with repository.connection() as db:
        run = db.execute("SELECT * FROM ingest_runs WHERE id=%s", (run_id,)).fetchone()
        if not run:
            raise CatalogError("Rodada não encontrada.", 404)
        stats = compute_run_stats(repository, db, run)
        db.execute(
            "INSERT INTO ingest_run_stats(run_id,final,payload) VALUES(%s,%s,%s) "
            "ON CONFLICT(run_id) DO UPDATE SET final=excluded.final,payload=excluded.payload,"
            "computed_at=now()",
            (run_id, stats["final"], Jsonb(stats)),
        )
    return stats


def finalize_pending(repository: Any, limit: int = 1) -> list[str]:
    """Fecha as estatísticas das rodadas já terminadas que ainda não as têm (mais novas antes)."""
    repository.initialize()
    with repository.connection() as db:
        pending = [
            row["id"]
            for row in db.execute(
                "SELECT r.id FROM ingest_runs r WHERE r.finished_at IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM ingest_run_stats s WHERE s.run_id=r.id AND s.final) "
                "ORDER BY r.created_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
        ]
    for run_id in pending:
        refresh_run_stats(repository, run_id)
    return pending


def backfill_stats(repository: Any, days: int = 30, force: bool = False) -> list[str]:
    """Calcula as estatísticas das rodadas terminadas da janela que ainda não as têm.

    Com ``force`` recalcula também as que já existem (por exemplo, após um campo novo).
    """
    repository.initialize()
    with repository.connection() as db:
        pending = [
            row["id"]
            for row in db.execute(
                "SELECT r.id FROM ingest_runs r WHERE r.finished_at IS NOT NULL "
                "AND r.created_at>now()-make_interval(days=>%s) AND (%s OR NOT EXISTS "
                "(SELECT 1 FROM ingest_run_stats s WHERE s.run_id=r.id AND s.final)) "
                "ORDER BY r.created_at DESC",
                (days, force),
            ).fetchall()
        ]
    for run_id in pending:
        refresh_run_stats(repository, run_id)
    return pending


def load_stats(repository: Any, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Estatísticas já gravadas das rodadas pedidas (as que não existem ficam de fora)."""
    repository.initialize()
    with repository.connection() as db:
        rows = db.execute(
            "SELECT run_id,payload FROM ingest_run_stats WHERE run_id=ANY(%s)", (run_ids,)
        ).fetchall()
    return {row["run_id"]: row["payload"] for row in rows}
