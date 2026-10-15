"""Execução em lote da sondagem e geração do relatório de viabilidade."""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from buscador_midias.discovery.probe import (
    DEFAULT_MAX_SITEMAP_FETCHES,
    ProbeStatus,
    SiteProbe,
    probe_site,
)
from buscador_midias.domain.site import Site
from buscador_midias.net.fetcher import Fetcher

REPORT_COLUMNS = (
    "site_key",
    "status",
    "sitemap_urls",
    "has_index",
    "has_news",
    "urls_sampled",
    "urls_other_hosts",
    "with_lastmod",
    "recent_24h",
    "recent_7d",
    "detail",
)


def probe_all(
    sites: Sequence[Site],
    fetcher: Fetcher,
    *,
    now: datetime,
    user_agent: str,
    workers: int = 8,
    max_sitemap_fetches: int = DEFAULT_MAX_SITEMAP_FETCHES,
    on_progress: Callable[[int, int, SiteProbe], None] | None = None,
) -> list[SiteProbe]:
    """Sonda vários sites em paralelo, preservando a ordem de entrada.

    Uma falha inesperada em um site vira ``ProbeStatus.ERROR`` para ele e não interrompe os
    demais. O intervalo por host é responsabilidade do ``fetcher`` (``ThrottledFetcher``).

    Args:
        sites: Fontes a sondar.
        fetcher: Porta HTTP compartilhada entre as threads (deve ser thread-safe).
        now: Instante de referência para contar URLs recentes.
        user_agent: Nome do nosso agente, usado nas regras do robots.txt.
        workers: Número de threads.
        max_sitemap_fetches: Orçamento de sitemaps lidos por site.
        on_progress: Chamada a cada site concluído com ``(concluídos, total, resultado)``. Roda
            sempre na thread que chamou ``probe_all``, então não precisa de trava.

    Returns:
        Um ``SiteProbe`` por site, na mesma ordem de ``sites``.
    """

    def run(site: Site) -> SiteProbe:
        try:
            return probe_site(
                site,
                fetcher,
                now=now,
                user_agent=user_agent,
                max_sitemap_fetches=max_sitemap_fetches,
            )
        except Exception as error:  # noqa: BLE001 - isola o site que falhou
            return SiteProbe(
                site_key=site.key,
                status=ProbeStatus.ERROR,
                sitemap_urls=(),
                has_index=False,
                has_news=False,
                urls_sampled=0,
                with_lastmod=0,
                recent_24h=0,
                recent_7d=0,
                detail=f"{type(error).__name__}: {error}",
            )

    results: list[SiteProbe | None] = [None] * len(sites)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, site): index for index, site in enumerate(sites)}
        for done, future in enumerate(as_completed(futures), start=1):
            probe = future.result()
            results[futures[future]] = probe
            if on_progress is not None:
                on_progress(done, len(sites), probe)
    return [probe for probe in results if probe is not None]


def summarize(probes: Sequence[SiteProbe]) -> dict[str, int]:
    """Conta os resultados por desfecho e por capacidade de descoberta.

    Args:
        probes: Resultados da sondagem.

    Returns:
        Contagens: ``total``, uma por ``ProbeStatus`` (valor do enum), ``with_news_sitemap``,
        ``with_lastmod`` e ``with_recent_24h`` (sites com ao menos uma URL nessa condição).
    """
    summary: dict[str, int] = {"total": len(probes)}
    for status in ProbeStatus:
        summary[status.value] = sum(1 for p in probes if p.status is status)
    summary["with_news_sitemap"] = sum(1 for p in probes if p.has_news)
    summary["with_lastmod"] = sum(1 for p in probes if p.with_lastmod > 0)
    summary["with_recent_24h"] = sum(1 for p in probes if p.recent_24h > 0)
    return summary


def write_probe_report(probes: Sequence[SiteProbe], path: Path) -> None:
    """Grava o relatório por site em CSV (UTF-8), um site por linha.

    Args:
        probes: Resultados da sondagem.
        path: Arquivo de saída; o diretório pai é criado se necessário.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        for probe in probes:
            writer.writerow(
                {
                    "site_key": probe.site_key,
                    "status": probe.status.value,
                    "sitemap_urls": " | ".join(probe.sitemap_urls),
                    "has_index": probe.has_index,
                    "has_news": probe.has_news,
                    "urls_sampled": probe.urls_sampled,
                    "urls_other_hosts": probe.urls_other_hosts,
                    "with_lastmod": probe.with_lastmod,
                    "recent_24h": probe.recent_24h,
                    "recent_7d": probe.recent_7d,
                    "detail": probe.detail,
                }
            )
