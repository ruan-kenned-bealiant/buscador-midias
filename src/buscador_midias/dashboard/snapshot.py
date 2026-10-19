"""Retrato do estado do fluxo, em um dicionário serializável em JSON, para alimentar o painel."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from buscador_midias.analysis.headline import headline_text
from buscador_midias.dashboard.schedule import build_schedule
from buscador_midias.discovery.stats import RunSummary, build_report
from buscador_midias.state.store import CorpusRun, EnrichRun, RecentUrl, StateStore

DEFAULT_HOURS = 48
DEFAULT_RECENT = 40
DEFAULT_MAX_SITES = 300
STALE_AFTER = timedelta(hours=3)
_RUNS_SHOWN = 30
_RECENT_RUNS_SHOWN = 15

_FUNNEL = (
    ("urls", "URLs vistas"),
    ("baseline", "Linha de base"),
    ("backlog", "Backlog"),
    ("known", "Já no corpus"),
    ("filtered", "Filtradas (forma da URL)"),
    ("new", "Novas"),
    ("pending", "Aguardando análise"),
    ("analyzed", "Analisadas (contexto)"),
    ("relevant", "Atendem ao critério (entidade)"),
    ("findings", "Achados adversos"),
    ("evidence", "Evidência capturada"),
    ("corpus", "Gravadas no corpus"),
)


def build_snapshot(
    store: StateStore,
    *,
    now: datetime,
    hours: int = DEFAULT_HOURS,
    recent: int = DEFAULT_RECENT,
    max_sites: int = DEFAULT_MAX_SITES,
    schedule_path: Path | None = None,
    progress_path: Path | None = None,
) -> dict[str, Any]:
    """Reúne tudo o que o painel mostra a partir do estado persistido.

    Só lê o estado. É pensado para ser chamado a cada poucos segundos enquanto outros processos
    (descoberta, enriquecimento) gravam.

    Args:
        store: Estado (pode estar aberto somente leitura).
        now: Instante de referência (UTC).
        hours: Janela da série por hora.
        recent: Quantas URLs novas aparecem no feed.
        max_sites: Limite de fontes listadas, com as que têm problema primeiro.
        schedule_path: JSON com as execuções previstas (opcional; veja ``schedule``).
        progress_path: Progresso e heartbeat da busca ativa, quando disponíveis.

    Returns:
        O retrato, com chaves ``status``, ``totals``, ``funnel``, ``runs``, ``hourly``, ``sites``,
        ``enrich``, ``corpus``, ``filter_reasons``, ``latency``, ``recent``,
        ``fallback_hosts`` e ``schedule``.
    """
    report = build_report(store)
    by_kind = store.count_urls_by_kind()
    enrichment = store.enrichment_counts()
    corpus_runs = store.corpus_runs()
    corpus_counts = store.corpus_write_counts()
    sites, sites_total = _sites(store, max_sites)
    return {
        "generated_at": now.isoformat(),
        "status": _status(store, report.runs, now, progress_path=progress_path),
        "totals": {
            "urls": sum(by_kind.values()),
            "by_kind": by_kind,
            "bright_data_requests": sum(run.bright_data_requests for run in report.runs),
        },
        "funnel": _funnel(by_kind, enrichment, corpus_counts["written"]),
        "runs": [_run(store, run) for run in report.runs[-_RUNS_SHOWN:]],
        "hourly": _hourly(store, now, hours),
        "sites": sites,
        "sites_total": sites_total,
        "enrich": _enrich(store, report.enrich_runs, report.enrich_cost_usd, enrichment, recent),
        "corpus": _corpus(corpus_runs, corpus_counts),
        "filter_reasons": report.filter_reasons,
        "latency": (
            {
                "count": report.latency.count,
                "median_hours": report.latency.median_hours,
                "p90_hours": report.latency.p90_hours,
            }
            if report.latency
            else None
        ),
        "recent": [_recent(item) for item in store.recent_new_urls(recent)],
        "fallback_hosts": list(report.fallback_hosts),
        "schedule": build_schedule(
            path=schedule_path,
            discovery_runs=store.list_runs(),
            now=now,
            enrich_runs=list(report.enrich_runs),
            corpus_runs=corpus_runs,
        ),
    }


def _live_progress(path: Path | None, run_id: int, now: datetime) -> dict[str, Any]:
    """Aceita somente heartbeat recente da mesma rodada, sem confiar em estado antigo."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        age = (now - datetime.fromisoformat(data["updated_at"])).total_seconds()
        if (data.get("run_id") == run_id and data.get("status") == "running"
                and 0 <= age <= 60 and type(data.get("sites_expected")) is int
                and data["sites_expected"] > 0):
            return data
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {}


def _status(
    store: StateStore, runs: tuple[RunSummary, ...], now: datetime,
    *, progress_path: Path | None = None,
) -> dict[str, Any]:
    """Situação da descoberta e do enriquecimento: rodando, ociosa, interrompida ou nunca."""
    discovery: dict[str, Any] = {"state": "never"}
    last_finished: datetime | None = None
    records = store.list_runs()
    if records:
        last = records[-1]
        finished = [r.finished_at for r in records if r.finished_at]
        last_finished = max(finished) if finished else None
        if last.finished_at is not None:
            discovery = {"state": "idle", "run_id": last.id}
        else:
            elapsed = now - last.started_at
            expected = runs[-2].sites if len(runs) >= 2 else None
            live = _live_progress(progress_path, last.id, now)
            if live:
                expected = live["sites_expected"]
            discovery = {
                "state": "stale" if elapsed > STALE_AFTER and not live else "running",
                "run_id": last.id,
                "started_at": last.started_at.isoformat(),
                "sites_done": store.sites_done(last.id),
                "sites_expected": expected,
                "sites_expected_exact": bool(live),
                "window_since": live.get("window_since"),
                "window_until": live.get("window_until"),
                "documents_completed": live.get("documents_completed"),
                "documents_queued": live.get("documents_queued"),
                "mechanisms_completed": live.get("mechanisms_completed"),
                "mechanisms_total": live.get("mechanisms_total"),
                "elapsed_seconds": int(elapsed.total_seconds()),
            }

    enrich: dict[str, Any] = {"state": "never"}
    enrich_runs = store.enrich_runs()
    if enrich_runs:
        last_enrich = enrich_runs[-1]
        if last_enrich.finished_at is None:
            elapsed = now - last_enrich.started_at
            enrich = {
                "state": "stale" if elapsed > STALE_AFTER else "running",
                "run_id": last_enrich.id,
                "elapsed_seconds": int(elapsed.total_seconds()),
            }
        else:
            enrich = {"state": "idle", "run_id": last_enrich.id}
    enrich_finished = [r.finished_at for r in enrich_runs if r.finished_at]
    return {
        "discovery": discovery,
        "enrich": enrich,
        "last_enrich_finished_at": max(enrich_finished).isoformat() if enrich_finished else None,
        "last_discovery_finished_at": last_finished.isoformat() if last_finished else None,
    }


def _funnel(
    by_kind: dict[str, int],
    enrichment: dict[str, Any],
    corpus_written: int,
) -> list[dict[str, Any]]:
    """Passos do funil, do total visto até a evidência capturada."""
    analysis = enrichment["analysis"]
    captured = sum(n for key, n in enrichment["evidence"].items() if key.startswith("captured:"))
    values = {
        "urls": sum(by_kind.values()),
        **{k: by_kind.get(k, 0) for k in by_kind},
        "pending": enrichment["pending_enrichment"],
        "analyzed": analysis["findings"] + analysis["no_finding"],
        "relevant": analysis["relevant"],
        "findings": analysis["findings"],
        "evidence": captured,
        "corpus": corpus_written,
    }
    return [{"key": key, "label": label, "value": values.get(key, 0)} for key, label in _FUNNEL]


def _run(store: StateStore, run: RunSummary) -> dict[str, Any]:
    """Uma rodada para a tabela e os gráficos."""
    duration = int((run.finished_at - run.started_at).total_seconds()) if run.finished_at else None
    return {
        "id": run.run_id,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": duration,
        "sites": run.sites,
        "sites_done": run.sites if run.finished_at else store.sites_done(run.run_id),
        "ok": run.ok,
        "failed": run.failed,
        "new": run.new,
        "baseline": run.baseline,
        "backlog": run.backlog,
        "known": run.known,
        "filtered": run.filtered,
        "seen": run.seen,
        "sitemaps_fetched": run.sitemaps_fetched,
        "sitemaps_skipped": run.sitemaps_skipped,
        "partial_sites": run.partial_sites,
        "sitemaps_failed": run.sitemaps_failed,
        "bright_data_requests": run.bright_data_requests,
        "via_sitemap": run.via_sitemap,
        "via_feed": run.via_feed,
        "via_paid": run.via_paid,
    }


def _hourly(store: StateStore, now: datetime, hours: int) -> list[dict[str, Any]]:
    """Série contínua por hora (UTC): novas descobertas e análises, com zeros nos vazios."""
    current = now.replace(minute=0, second=0, microsecond=0)
    start = current - timedelta(hours=hours)
    new = store.new_urls_per_hour(since=start)
    analyses = store.analyzed_per_hour(since=start)
    series = []
    for offset in range(hours + 1):
        key = (start + timedelta(hours=offset)).strftime("%Y-%m-%dT%H")
        verdicts = analyses.get(key, {})
        series.append(
            {
                "hour": key,
                "new": new.get(key, 0),
                "meets": verdicts.get("meets", 0),
                "not_meets": verdicts.get("not_meets", 0),
            }
        )
    return series


def _sites(store: StateStore, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Fontes com o último desfecho e as contagens; problemas primeiro, depois as mais ativas."""
    last: dict[str, tuple[int, Any]] = {}
    for run_id, record in store.all_site_runs():
        last[record.site_key] = (run_id, record)
    urls = store.url_counts_by_site()
    last_new = store.last_new_by_site()
    fallback = store.fallback_hosts()
    rows = []
    for site, (run_id, record) in last.items():
        kinds = urls.get(site, {})
        stamp = last_new.get(site)
        rows.append(
            {
                "site": site,
                "status": record.status,
                "detail": record.detail,
                "source": record.source,
                "partial_failed": record.sitemaps_failed,
                "last_run_id": run_id,
                "new": kinds.get("new", 0),
                "urls": kinds,
                "last_new_at": stamp.isoformat() if stamp else None,
                "fallback": any(host in site or site in host for host in fallback),
            }
        )
    rows.sort(
        key=lambda row: (
            row["status"] == "ok",
            row["partial_failed"] == 0,
            -row["new"],
            row["site"],
        )
    )
    return rows[:limit], len(rows)


def _recent(item: RecentUrl) -> dict[str, Any]:
    """Uma URL do feed: manchete (título ou slug) e situação da análise."""
    return {
        "first_seen_at": item.first_seen_at.isoformat(),
        "site": item.site_key,
        "url": item.url,
        "headline": headline_text(item.url, item.title),
        "status": item.status,
        "themes": list(item.themes),
    }


def _corpus(runs: list[CorpusRun], counts: dict[str, int]) -> dict[str, Any]:
    """Gravação no corpus: totais e as últimas execuções."""
    return {
        "written": counts["written"],
        "created": counts["created"],
        "runs": [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "candidates": r.totals.candidates,
                "created": r.totals.created,
                "existing": r.totals.existing,
                "evidence_updated": r.totals.evidence_updated,
                "entities": r.totals.entities,
                "keywords": r.totals.keywords,
                "failed": r.totals.failed,
                "rewritten": r.totals.rewritten,
            }
            for r in runs[-_RECENT_RUNS_SHOWN:]
        ],
    }


def _enrich(
    store: StateStore,
    runs: tuple[EnrichRun, ...],
    cost_total: float | None,
    counts: dict[str, Any],
    recent: int,
) -> dict[str, Any]:
    """Execuções, custo, contagens, motivos de rejeição e achados recentes do enriquecimento."""
    evidence = counts["evidence"]
    reused = evidence.get("captured:direct", 0)
    paid = evidence.get("captured:brightdata", 0)
    return {
        "runs": [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "model": r.model,
                "prompt_version": r.prompt_version,
                "evidence_mode": r.evidence_mode,
                "urls": r.totals.urls,
                "fetched_ok": r.totals.fetched_ok,
                "fetched_blocked": r.totals.fetched_blocked,
                "fetched_other": r.totals.fetched_other,
                "analyzed": r.totals.analyzed,
                "findings": r.totals.findings,
                "evidence_reused": r.totals.evidence_reused,
                "evidence_brightdata": r.totals.evidence_brightdata,
                "evidence_failed": r.totals.evidence_failed,
                "bright_data_requests": r.totals.bright_data_requests,
                "prompt_tokens": r.totals.prompt_tokens,
                "completion_tokens": r.totals.completion_tokens,
                "cost_usd": r.totals.cost_usd,
                "errors": r.totals.errors,
            }
            for r in runs[-_RECENT_RUNS_SHOWN:]
        ],
        "cost_total_usd": cost_total,
        "counts": counts,
        "reuse_rate": round(reused / (reused + paid), 4) if reused + paid else None,
        "evidence_reasons": store.evidence_rejection_reasons(),
        "findings": [
            {
                "analyzed_at": f.analyzed_at.isoformat(),
                "site": f.site_key,
                "url": f.url,
                "headline": f.headline,
                "themes": list(f.themes),
                "adverse_entities": list(f.adverse_entities),
                "adverse_fact": f.adverse_fact,
                "confidence": f.confidence,
                "basis": f.basis,
                "evidence_status": f.evidence_status,
                "evidence_provider": f.evidence_provider,
            }
            for f in store.recent_findings(recent)
        ],
    }
