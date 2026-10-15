"""Sondagem de viabilidade da descoberta: o que cada site oferece via robots.txt e sitemaps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from buscador_midias.discovery.explore import (
    DEFAULT_MAX_SITEMAP_FETCHES,
    Exploration,
    ProbeStatus,
    explore_site,
)
from buscador_midias.domain.site import Site
from buscador_midias.domain.sitemap import SitemapKind
from buscador_midias.net.fetcher import Fetcher

__all__ = ["DEFAULT_MAX_SITEMAP_FETCHES", "ProbeStatus", "SiteProbe", "probe_site"]


@dataclass(frozen=True, slots=True)
class SiteProbe:
    """Resumo do que a descoberta por sitemap consegue extrair de um site.

    Attributes:
        site_key: Identidade da fonte (``Site.key``).
        status: Desfecho da sondagem.
        sitemap_urls: Sitemaps de entrada (declarados no robots ou o fallback que respondeu).
        has_index: Se algum dos sitemaps de entrada era um índice.
        has_news: Se algum sitemap lido era um news sitemap.
        urls_sampled: URLs da fonte vistas nos sitemaps lidos (amostra, não o total do site).
        urls_other_hosts: URLs lidas que pertencem a outro host (ex.: o sitemap de
            ``primerahora.com`` lista ``elnuevodia.com``); sinal de catálogo ou site divergente.
        with_lastmod: URLs com data (``lastmod`` ou data de publicação).
        recent_24h: URLs com data nas últimas 24 horas.
        recent_7d: URLs com data nos últimos 7 dias.
        detail: Explicação curta do problema, quando houve.
    """

    site_key: str
    status: ProbeStatus
    sitemap_urls: tuple[str, ...]
    has_index: bool
    has_news: bool
    urls_sampled: int
    with_lastmod: int
    recent_24h: int
    recent_7d: int
    detail: str
    urls_other_hosts: int = 0


def probe_site(
    site: Site,
    fetcher: Fetcher,
    *,
    now: datetime,
    user_agent: str,
    max_sitemap_fetches: int = DEFAULT_MAX_SITEMAP_FETCHES,
) -> SiteProbe:
    """Sonda um site: robots.txt, sitemaps declarados (ou caminhos padrão) e amostra de URLs.

    Args:
        site: Fonte a sondar.
        fetcher: Porta HTTP (use a versão com intervalo por host em produção).
        now: Instante de referência para contar URLs recentes.
        user_agent: Nome do nosso agente, usado nas regras do robots.txt.
        max_sitemap_fetches: Orçamento de sitemaps lidos por site.

    Returns:
        O resumo da sondagem; nunca levanta por causa do conteúdo ou da rede do site.
    """
    exploration = explore_site(
        site, fetcher, user_agent=user_agent, max_sitemap_fetches=max_sitemap_fetches
    )
    if exploration.status is not ProbeStatus.OK:
        return _failure(site, exploration.status, exploration.detail)
    return _summarize(site, exploration, now)


def _summarize(site: Site, exploration: Exploration, now: datetime) -> SiteProbe:
    """Consolida os sitemaps lidos em um ``SiteProbe``."""
    seen = [entry for item in exploration.sitemaps for entry in item.sitemap.entries]
    entries = [entry for entry in seen if site.owns(entry.loc)]
    stamps = [entry.timestamp for entry in entries if entry.timestamp is not None]
    entry_sitemaps = [item for item in exploration.sitemaps if item.is_entry]
    return SiteProbe(
        site_key=site.key,
        status=ProbeStatus.OK,
        sitemap_urls=tuple(item.url for item in entry_sitemaps),
        has_index=any(item.sitemap.kind is SitemapKind.INDEX for item in entry_sitemaps),
        has_news=any(item.sitemap.has_news for item in exploration.sitemaps),
        urls_sampled=len(entries),
        urls_other_hosts=len(seen) - len(entries),
        with_lastmod=len(stamps),
        recent_24h=sum(1 for s in stamps if now - s <= timedelta(hours=24)),
        recent_7d=sum(1 for s in stamps if now - s <= timedelta(days=7)),
        detail="",
    )


def _failure(site: Site, status: ProbeStatus, detail: str) -> SiteProbe:
    """Cria um ``SiteProbe`` sem dados de sitemap (falhas e desfechos antecipados)."""
    return SiteProbe(
        site_key=site.key,
        status=status,
        sitemap_urls=(),
        has_index=False,
        has_news=False,
        urls_sampled=0,
        with_lastmod=0,
        recent_24h=0,
        recent_7d=0,
        detail=detail,
    )
