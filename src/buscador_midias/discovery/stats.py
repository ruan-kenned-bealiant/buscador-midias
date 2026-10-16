"""Estatísticas das rodadas de descoberta: o que cada rodada rendeu e onde há sinais de problema."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from buscador_midias.state.store import (
    CorpusRun,
    EnrichRun,
    RunRecord,
    SiteRunRecord,
    StateStore,
)


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Totais de uma rodada, somando todos os sites.

    Attributes:
        run_id: Identificador da rodada.
        started_at: Início (UTC).
        finished_at: Fim (UTC), ou ``None`` se não terminou.
        sites: Sites processados.
        ok: Sites com desfecho ``ok``.
        failed: Sites com qualquer outro desfecho.
        new: URLs novas.
        baseline: URLs registradas como linha de base.
        backlog: URLs inéditas mas antigas demais para contar como novas.
        known: URLs inéditas que o corpus já tinha.
        seen: URLs dos sites vistas nos sitemaps lidos.
        sitemaps_fetched: Sitemaps requisitados.
        sitemaps_skipped: Sitemaps dispensados por ``lastmod`` inalterado.
        partial_sites: Sites ``ok`` em que algum sitemap falhou (leitura parcial).
        sitemaps_failed: Sitemaps que falharam nos sites ``ok``.
        filtered: URLs frescas descartadas pela forma da URL (L0).
        bright_data_requests: Chamadas pagas ao Bright Data na rodada.
        via_sitemap: Sites cujas URLs vieram do sitemap gratuito.
        via_feed: Sites cujas URLs vieram do feed.
        via_paid: Sites cujas URLs vieram do sitemap pelo Bright Data.
    """

    run_id: int
    started_at: datetime
    finished_at: datetime | None
    sites: int
    ok: int
    failed: int
    new: int
    baseline: int
    backlog: int
    known: int
    seen: int
    sitemaps_fetched: int
    sitemaps_skipped: int
    partial_sites: int
    sitemaps_failed: int
    filtered: int
    bright_data_requests: int = 0
    via_sitemap: int = 0
    via_feed: int = 0
    via_paid: int = 0


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Atraso entre a publicação de uma notícia (news sitemap) e o momento em que a vimos.

    Attributes:
        count: Quantas URLs novas têm data de publicação.
        median_hours: Mediana, em horas.
        p90_hours: Percentil 90 (posto mais próximo), em horas.
    """

    count: int
    median_hours: float
    p90_hours: float


@dataclass(frozen=True, slots=True)
class Report:
    """Visão consolidada de todas as rodadas.

    Attributes:
        runs: Um resumo por rodada, da mais antiga à mais nova.
        sites_without_new: Sites julgáveis (já passaram da linha de base) que nunca trouxeram uma
            URL inédita: sinal de sitemap parado ou ``lastmod`` que não reflete publicações.
        latency: Atraso de publicação até a descoberta, quando há dados.
        fallback_hosts: Hosts que dependem do fallback pago.
        filter_reasons: URLs descartadas pela forma da URL (L0), por motivo (para auditar o filtro).
        enrich_runs: Execuções do enriquecimento (texto, análise de contexto, evidência).
        enrich_cost_usd: Custo total da análise de contexto (``None`` se nunca informado).
        enrichment_counts: Contagens de conteúdo, análises, evidências e pendências.
        evidence_reasons: Por que o HTML direto não foi reaproveitado, por motivo.
        corpus_runs: Execuções da gravação no corpus de Mídias IA.
        corpus_counts: Achados gravados (``written``) e criados por nós (``created``).
    """

    runs: tuple[RunSummary, ...]
    sites_without_new: tuple[str, ...]
    latency: LatencyStats | None
    fallback_hosts: tuple[str, ...]
    filter_reasons: dict[str, int]
    enrich_runs: tuple[EnrichRun, ...] = ()
    enrich_cost_usd: float | None = None
    enrichment_counts: dict[str, Any] | None = None
    evidence_reasons: dict[str, int] | None = None
    corpus_runs: tuple[CorpusRun, ...] = ()
    corpus_counts: dict[str, int] | None = None


def build_report(store: StateStore) -> Report:
    """Calcula o relatório a partir do estado persistido."""
    by_run: dict[int, list[SiteRunRecord]] = defaultdict(list)
    by_site: dict[str, list[SiteRunRecord]] = defaultdict(list)
    for run_id, record in store.all_site_runs():
        by_run[run_id].append(record)
        by_site[record.site_key].append(record)

    runs = tuple(_summarize(run, by_run.get(run.id, [])) for run in store.list_runs())
    enrich_runs = tuple(store.enrich_runs())
    return Report(
        runs=runs,
        sites_without_new=_sites_without_new(by_site),
        latency=_latency(store.new_url_latencies_hours()),
        fallback_hosts=tuple(sorted(store.fallback_hosts())),
        filter_reasons=store.filtered_by_reason(),
        enrich_runs=enrich_runs,
        enrich_cost_usd=_enrich_cost(enrich_runs),
        enrichment_counts=store.enrichment_counts(),
        evidence_reasons=store.evidence_rejection_reasons(),
        corpus_runs=tuple(store.corpus_runs()),
        corpus_counts=store.corpus_write_counts(),
    )


def format_report(report: Report) -> str:
    """Texto legível do relatório, para o terminal."""
    lines: list[str] = []
    for run in report.runs:
        finished = run.finished_at.strftime("%H:%M") if run.finished_at else "em andamento"
        lines.append(
            f"rodada {run.run_id}: {run.started_at:%Y-%m-%d %H:%M} -> {finished} | "
            f"sites {run.sites} (ok {run.ok}, falhas {run.failed}) | novas {run.new} | "
            f"linha de base {run.baseline} | backlog {run.backlog} | no corpus {run.known} | "
            f"filtradas {run.filtered} | "
            f"sitemaps lidos {run.sitemaps_fetched}, pulados {run.sitemaps_skipped}"
            + (
                f" | leitura parcial {run.partial_sites} (sitemaps com falha {run.sitemaps_failed})"
                if run.partial_sites
                else ""
            )
            + (f" | bright data {run.bright_data_requests} req" if run.bright_data_requests else "")
            + (
                f" | fontes: sitemap {run.via_sitemap}, feed {run.via_feed}, "
                f"sitemap pago {run.via_paid}"
                if run.via_feed or run.via_paid
                else ""
            )
        )
    if report.latency is not None:
        lat = report.latency
        lines.append(
            f"latência publicação -> descoberta (n={lat.count}): "
            f"mediana {lat.median_hours:.1f} h, p90 {lat.p90_hours:.1f} h"
        )
    if report.sites_without_new:
        lines.append(
            "sites sem nenhuma URL inédita após a linha de base: "
            + ", ".join(report.sites_without_new)
        )
    if report.filter_reasons:
        ranked = sorted(report.filter_reasons.items(), key=lambda item: -item[1])
        lines.append(
            "descartes pela forma da URL (L0): "
            + ", ".join(f"{reason} {count}" for reason, count in ranked)
        )
    lines.extend(_enrich_lines(report))
    lines.extend(_corpus_lines(report))
    if report.fallback_hosts:
        lines.append("hosts em fallback (Bright Data): " + ", ".join(report.fallback_hosts))
    return "\n".join(lines) if lines else "nenhuma rodada registrada"


def _enrich_lines(report: Report) -> list[str]:
    """Linhas do enriquecimento: uma por execução, o acumulado e os motivos de rejeição."""
    if not report.enrich_runs:
        return []
    lines = []
    for run in report.enrich_runs:
        t = run.totals
        cost = f"US$ {t.cost_usd:.4f}" if t.cost_usd is not None else "custo não informado"
        lines.append(
            f"enriquecimento #{run.id} ({run.model}, {run.prompt_version}, {run.evidence_mode}): "
            f"URLs {t.urls} | baixadas ok {t.fetched_ok}, bloqueadas {t.fetched_blocked}, "
            f"outras falhas {t.fetched_other} | analisadas {t.analyzed} | "
            f"achados adversos {t.findings} | "
            f"evidências reaproveitadas {t.evidence_reused}, Bright Data {t.evidence_brightdata}, "
            f"falhas {t.evidence_failed} | Bright Data {t.bright_data_requests} req | "
            f"tokens {t.prompt_tokens}/{t.completion_tokens} | {cost}"
        )
    counts = report.enrichment_counts or {}
    evidence = counts.get("evidence", {})
    total = (
        f"custo total US$ {report.enrich_cost_usd:.4f}"
        if report.enrich_cost_usd is not None
        else "custo total não informado"
    )
    lines.append(
        f"enriquecimento acumulado: matérias com entidade "
        f"{counts.get('analysis', {}).get('relevant', 0)}, "
        f"achados adversos {counts.get('analysis', {}).get('findings', 0)}, "
        f"evidências capturadas (direto {evidence.get('captured:direct', 0)}, "
        f"Bright Data {evidence.get('captured:brightdata', 0)}), "
        f"URLs aguardando análise {counts.get('pending_enrichment', 0)}, "
        f"achados aguardando evidência {counts.get('pending_evidence', 0)} | {total}"
    )
    if report.evidence_reasons:
        ranked = sorted(report.evidence_reasons.items(), key=lambda item: -item[1])
        lines.append(
            "motivos de não reaproveitar o HTML: " + ", ".join(f"{k} {v}" for k, v in ranked)
        )
    return lines


def _corpus_lines(report: Report) -> list[str]:
    """Linhas da gravação no corpus: uma por execução e o acumulado."""
    if not report.corpus_runs:
        return []
    lines = []
    for run in report.corpus_runs:
        t = run.totals
        lines.append(
            f"corpus #{run.id}: candidatos {t.candidates} | criados {t.created}, "
            f"já existiam {t.existing}, evidências atualizadas {t.evidence_updated}, "
            f"refeitos {t.rewritten} | entidades {t.entities}, temas {t.keywords} | "
            f"falhas {t.failed}"
        )
    counts = report.corpus_counts or {}
    lines.append(
        f"corpus acumulado: gravados {counts.get('written', 0)}, "
        f"criados por nós {counts.get('created', 0)}"
    )
    return lines


def _enrich_cost(runs: tuple[EnrichRun, ...]) -> float | None:
    """Soma do custo das execuções que o informaram (``None`` se nenhuma informou)."""
    costs = [r.totals.cost_usd for r in runs if r.totals.cost_usd is not None]
    return round(sum(costs), 8) if costs else None


def _summarize(run: RunRecord, records: list[SiteRunRecord]) -> RunSummary:
    """Soma os registros de uma rodada."""
    return RunSummary(
        run_id=run.id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        bright_data_requests=run.bright_data_requests,
        sites=len(records),
        ok=sum(1 for r in records if r.status == "ok"),
        failed=sum(1 for r in records if r.status != "ok"),
        new=sum(r.new_urls for r in records),
        baseline=sum(r.baseline_urls for r in records),
        backlog=sum(r.backlog_urls for r in records),
        known=sum(r.known_urls for r in records),
        seen=sum(r.seen_urls for r in records),
        sitemaps_fetched=sum(r.sitemaps_fetched for r in records),
        sitemaps_skipped=sum(r.sitemaps_skipped for r in records),
        partial_sites=sum(1 for r in records if r.status == "ok" and r.sitemaps_failed),
        sitemaps_failed=sum(r.sitemaps_failed for r in records if r.status == "ok"),
        filtered=sum(r.filtered_urls for r in records),
        via_sitemap=sum(1 for r in records if r.source == "sitemap"),
        via_feed=sum(1 for r in records if r.source == "feed"),
        via_paid=sum(1 for r in records if r.source == "sitemap_bd"),
    )


def _sites_without_new(by_site: dict[str, list[SiteRunRecord]]) -> tuple[str, ...]:
    """Sites com ao menos uma rodada ``ok`` depois da linha de base e nenhuma URL inédita."""
    flagged: list[str] = []
    for site, records in sorted(by_site.items()):
        ok_runs = [r for r in records if r.status == "ok"]
        judged = ok_runs[1:]  # a primeira rodada ok é a linha de base
        if judged and sum(r.new_urls + r.known_urls + r.backlog_urls for r in judged) == 0:
            flagged.append(site)
    return tuple(flagged)


def _latency(hours: list[float]) -> LatencyStats | None:
    """Mediana e p90 (posto mais próximo) das latências, ou ``None`` sem dados."""
    if not hours:
        return None
    ordered = sorted(hours)
    rank = max(math.ceil(0.9 * len(ordered)) - 1, 0)
    return LatencyStats(
        count=len(ordered), median_hours=statistics.median(ordered), p90_hours=ordered[rank]
    )
