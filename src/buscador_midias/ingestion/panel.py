"""Dados da tela Painel: última rodada, histórico, estado atual, falhas, saúde e auditoria.

Lê números já calculados (``ingest_run_stats``) e consultas pequenas; nada varre as tarefas.
O que ainda não foi calculado é completado sob demanda, com teto por requisição.
"""

from __future__ import annotations

import re
import shutil
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion.cold_links import cold_link_ids
from buscador_midias.ingestion.flow import next_slot
from buscador_midias.ingestion.run_stats import load_stats, refresh_run_stats

MAX_ON_DEMAND = 3  # rodadas sem número calculado que uma requisição completa
OPEN_TTL = 30  # segundos até recalcular a rodada aberta
COLD_TTL = 900  # a contagem de links frios custa ~1,5 s: vale por 15 minutos
TOP_FAILURES = 10
HEALTH_ACTIONS = dict(
    health_inactivate="inactivated",
    health_reactivate="reactivated",
    health_transport="transport",
    health_delete="deleted",
)
CORPUS_SCHEMA = "public"
CORPUS_TTL = 600  # o total do corpus custa ~1 s de contagem: vale por 10 minutos
_cold_cache: dict[str, tuple[float, int]] = {}
_corpus_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any] | None]] = {}


def _cold_links(repository: Any, db: Any) -> int:
    key = repository.store.schema
    cached = _cold_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    count = len(cold_link_ids(db))
    _cold_cache[key] = (time.monotonic() + COLD_TTL, count)
    return count


def _corpus(repository: Any, db: Any, schema: str, days: int, zone: str) -> dict[str, Any] | None:
    """Total do corpus ao fim de cada dia da janela, com o que entrou em cada um.

    Reconstrói o total acumulado de trás para frente (total atual menos o que entrou depois),
    então não depende de guardar histórico. Devolve ``None`` se a tabela não existe.
    """
    key = (repository.store.schema, schema, days)
    cached = _corpus_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    value: dict[str, Any] | None = None
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        table = f'"{schema}".midias_ia_articles'
        if db.execute("SELECT to_regclass(%s) IS NOT NULL ok", (table,)).fetchone()["ok"]:
            tz = ZoneInfo(zone)
            today = datetime.now(tz).date()
            first = today - timedelta(days=days - 1)
            rows = {
                row["day"]: row
                for row in db.execute(
                    "SELECT (created_at AT TIME ZONE %s)::date AS day,count(*) AS added,"  # noqa: S608
                    "count(*) FILTER (WHERE origin='crawler') AS crawler "
                    f"FROM {table} WHERE created_at>=%s GROUP BY 1",
                    (zone, datetime.combine(first, datetime.min.time(), tzinfo=tz)),
                ).fetchall()
            }
            total = db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608
            daily = []
            running = total
            for offset in range(days):
                day = today - timedelta(days=offset)
                row = rows.get(day, {})
                daily.append(
                    dict(
                        day=day.isoformat(),
                        added=row.get("added", 0),
                        crawler=row.get("crawler", 0),
                        total=running,
                    )
                )
                running -= row.get("added", 0)
            value = dict(total=total, daily=daily[::-1])
    _corpus_cache[key] = (time.monotonic() + CORPUS_TTL, value)
    return value


def _stale(stats: dict[str, Any], now: datetime) -> bool:
    computed = stats.get("computed_at")
    return not computed or (now - datetime.fromisoformat(computed)).total_seconds() > OPEN_TTL


def _history(repository: Any, db: Any, days: int) -> tuple[list[dict[str, Any]], int]:
    """Estatísticas das rodadas da janela; calcula as que faltam (até o teto) e as abertas."""
    runs = db.execute(
        "SELECT id,finished_at FROM ingest_runs WHERE created_at>now()-make_interval(days=>%s) "
        "ORDER BY created_at",
        (days,),
    ).fetchall()
    ids = [run["id"] for run in runs]
    stats = load_stats(repository, ids)
    now = datetime.now(UTC)
    budget = MAX_ON_DEMAND
    pending = 0
    for run in runs:
        known = stats.get(run["id"])
        needed = known is None or (run["finished_at"] is None and _stale(known, now))
        if known is not None and known["final"] != (run["finished_at"] is not None):
            needed = True
        if not needed:
            continue
        if budget:
            stats[run["id"]] = refresh_run_stats(repository, run["id"])
            budget -= 1
        elif known is None:
            pending += 1
    return [stats[i] for i in ids if i in stats], pending


def _failures(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: Counter[tuple[str, str]] = Counter()
    for run in runs:
        for item in run["failures"]:
            merged[(item["stage"], item["reason"])] += item["n"]
    total = sum(merged.values())
    return [
        dict(stage=stage, reason=reason, n=n, pct=round(100 * n / total, 1))
        for (stage, reason), n in sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[
            :TOP_FAILURES
        ]
    ]


def _audit(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Prova do corte pelo topo: só conta rodadas em que ele esteve ativo (``enforce``).

    Em modo sombra os "cortes" são simulações de uma fase anterior das regras; misturá-los
    faria um erro já corrigido aparecer como perda de hoje.
    """
    totals: Counter[str] = Counter()
    for run in (r for r in runs if r.get("head_filter") == "enforce"):
        totals.update({k: v for k, v in run["audit"].items() if isinstance(v, int)})
    reads = totals["discovery_reads"]

    def pct(part: int, whole: int) -> float | None:
        return round(100 * part / whole, 1) if whole else None

    return dict(
        sampled=totals["sampled"],
        would_skip=totals["would_skip"],
        missed=totals["missed"],
        loss_pct=pct(totals["missed"], totals["would_skip"]),
        cut_pct=pct(totals["head_cut"], reads),
        not_modified_pct=pct(totals["not_modified"], reads),
        contradicted=totals["contradicted"],
        discovery_reads=reads,
        cold_skipped=runs[-1]["adaptive_skipped"] if runs else 0,
    )


def _has_health(db: Any) -> bool:
    """A rotina de saúde cria as próprias tabelas; sem ela o painel só omite o bloco."""
    return db.execute("SELECT to_regclass('health_runs') IS NOT NULL ok").fetchone()["ok"]


def _health(db: Any, days: int, zone: str) -> dict[str, Any]:
    rows = db.execute(
        "SELECT (happened_at AT TIME ZONE %s)::date AS day,action,count(*) AS n FROM events "
        "WHERE action=ANY(%s) AND happened_at>now()-make_interval(days=>%s) GROUP BY 1,2",
        (zone, list(HEALTH_ACTIONS), days),
    ).fetchall()
    by_day: dict[Any, dict[str, int]] = {}
    for row in rows:
        by_day.setdefault(row["day"], {})[HEALTH_ACTIONS[row["action"]]] = row["n"]
    empty = dict.fromkeys(HEALTH_ACTIONS.values(), 0)
    today = datetime.now(ZoneInfo(zone)).date()
    daily = [
        dict(day=day.isoformat(), **{**empty, **by_day.get(day, {})})
        for day in (today - timedelta(days=offset) for offset in range(days - 1, -1, -1))
    ]
    last: dict[str, Any] = {}
    for kind in ("active", "recovery") if _has_health(db) else ():
        run = db.execute(
            "SELECT id,status,created_at,finished_at,total FROM health_runs WHERE kind=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if not run:
            continue
        counts = {
            row["k"]: row["n"]
            for row in db.execute(
                "SELECT (CASE WHEN state='completed' THEN category ELSE state END) k,count(*) n "
                "FROM health_results WHERE run_id=%s GROUP BY 1",
                (run["id"],),
            ).fetchall()
        }
        last[kind] = dict(
            status=run["status"],
            started_at=run["created_at"].isoformat(),
            finished_at=run["finished_at"].isoformat() if run["finished_at"] else None,
            total=run["total"],
            counts=counts,
        )
    return dict(daily=daily, last=last)


def _now(repository: Any, db: Any, settings: dict[str, Any]) -> dict[str, Any]:
    config = settings["config"]
    open_runs = [
        row["id"]
        for row in db.execute("SELECT id FROM ingest_runs WHERE finished_at IS NULL").fetchall()
    ]
    queues = (
        db.execute(
            "SELECT stage,state,count(*) n FROM ingest_jobs WHERE run_id=ANY(%s) "
            "AND state IN ('pending','queued','running','retry') GROUP BY 1,2 ORDER BY 1,2",
            (open_runs,),
        ).fetchall()
        if open_runs
        else []
    )
    online = db.execute(
        "SELECT count(*) n FROM ingest_workers WHERE heartbeat_at>now()-interval '2 minutes'"
    ).fetchone()["n"]
    catalog = db.execute(
        "SELECT count(*) FILTER (WHERE l.active=1 AND s.active=1) active_links,"
        "count(*) FILTER (WHERE l.active=0 OR s.active=0) inactive_links,"
        "count(DISTINCT l.source_id) FILTER (WHERE l.active=1 AND s.active=1) active_sites "
        "FROM links l JOIN sites s ON s.id=l.source_id "
        "WHERE l.deleted_at IS NULL AND s.deleted_at IS NULL"
    ).fetchone()
    zone = config.get("timezone", "America/Sao_Paulo")
    upcoming = (
        next_slot(datetime.now(UTC), zone, config.get("schedule_hours", []))
        if config.get("enabled") and config.get("schedule_mode", "clock") == "clock"
        else None
    )
    health_next = (
        {
            row["kind"]: row["next_run_at"].isoformat() if row["enabled"] else None
            for row in db.execute("SELECT kind,enabled,next_run_at FROM health_settings").fetchall()
        }
        if _has_health(db)
        else {}
    )
    return dict(
        open_run=open_runs[0] if open_runs else None,
        queues=[dict(r) for r in queues],
        workers_online=online,
        disk_free_gb=round(shutil.disk_usage(Path(__file__).resolve().parents[3]).free / 1e9, 1),
        next_round=upcoming.isoformat() if upcoming else None,
        next_health=health_next,
        catalog=dict(catalog, cold_links=_cold_links(repository, db)),
        settings={
            key: config.get(key)
            for key in (
                "head_filter",
                "head_audit_rate",
                "adaptive_polling",
                "host_concurrency",
                "host_concurrency_max",
                "schedule_hours",
                "timezone",
            )
        },
    )


def build_panel(
    repository: Any, days: int = 7, corpus_schema: str = CORPUS_SCHEMA
) -> dict[str, Any]:
    """Monta tudo o que a tela Painel mostra para a janela de ``days`` dias."""
    if type(days) is not int or not 1 <= days <= 30:
        raise CatalogError("Informe a janela de 1 a 30 dias.")
    repository.initialize()
    settings = repository.settings()
    zone = settings["config"].get("timezone", "America/Sao_Paulo")
    with repository.connection() as db:
        runs, pending = _history(repository, db, days)
        result = dict(
            generated_at=datetime.now(UTC).isoformat(),
            days=days,
            last=runs[-1] if runs else None,
            previous=runs[-2] if len(runs) > 1 else None,
            runs=runs,
            pending_stats=pending,
            failures=_failures(runs),
            audit=_audit(runs),
            health=_health(db, days, zone),
            corpus=_corpus(repository, db, corpus_schema, days, zone),
            now=_now(repository, db, settings),
        )
    return result
