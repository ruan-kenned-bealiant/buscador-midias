"""Fontes de descoberta de um site e a cascata que as tenta em ordem.

A ordem é a do custo: sitemap gratuito, depois feed gratuito e, só para quem bloqueia o acesso
gratuito, o sitemap pelo Bright Data (pago). A primeira fonte que traz URLs encerra a tentativa.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from buscador_midias.discovery.explore import (
    DEFAULT_MAX_SITEMAP_FETCHES,
    ExploredSitemap,
    ProbeStatus,
    explore_site,
)
from buscador_midias.discovery.feed_explore import explore_feed
from buscador_midias.domain.dated_url import render_window
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.domain.site import Site
from buscador_midias.domain.sitemap import SitemapRef
from buscador_midias.domain.strategy import CandidateReport, StrategyKind
from buscador_midias.net.fetcher import Fetcher

SkipChild = Callable[[SitemapRef], bool]

# Quanto mais baixo, mais informativa a falha: bloqueio > erro > robots > ausência.
_FAILURE_RANK = {
    ProbeStatus.BLOCKED: 0,
    ProbeStatus.ERROR: 1,
    ProbeStatus.ROBOTS_DISALLOWED: 2,
    ProbeStatus.NO_SITEMAP: 3,
}


class SourceKind(StrEnum):
    """De onde vieram as URLs de um site em uma rodada."""

    SITEMAP = "sitemap"
    FEED = "feed"
    SITEMAP_PAID = "sitemap_bd"


@dataclass(frozen=True, slots=True)
class SourceFind:
    """O que uma fonte de descoberta achou (ou por que falhou).

    Attributes:
        kind: A fonte que produziu o resultado.
        status: ``OK`` se trouxe URLs; senão o motivo da falha.
        detail: Explicação curta da falha (vazio quando ``OK``).
        urls: URLs da fonte, sem repetição (variantes de uma URL contam uma vez).
        sitemaps: Sitemaps lidos (vazio para o feed); servem para lembrar o ``lastmod`` deles.
        fetched: Requisições feitas (conta no orçamento de sitemaps).
        skipped: Sitemaps filhos dispensados por ``lastmod`` inalterado.
        failed: Sitemaps que falharam numa exploração ``OK`` (cobertura parcial).
        failure_detail: A falha mais informativa dentre as de ``failed``.
    """

    kind: SourceKind
    status: ProbeStatus
    detail: str
    urls: tuple[DiscoveredUrl, ...]
    sitemaps: tuple[ExploredSitemap, ...] = ()
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    failure_detail: str = ""

    @property
    def ok(self) -> bool:
        """Se a fonte serviu."""
        return self.status is ProbeStatus.OK


class DiscoverySource(Protocol):
    """Uma forma de descobrir as URLs de um site."""

    kind: SourceKind

    def applies(self, previous: Sequence[SourceFind]) -> bool:
        """Se vale tentar esta fonte, dado o que as anteriores da cascata já mostraram."""
        ...

    def find(self, site: Site, *, skip_child: SkipChild | None = None) -> SourceFind:
        """Procura as URLs do site."""
        ...


class SitemapSource:
    """Sitemap, lido pelo fetcher dado (gratuito ou pago).

    A versão paga só entra depois de um bloqueio do sitemap gratuito, ou sempre (``force``) para
    um host que já sabemos que bloqueia: ali o acesso gratuito nem é tentado.
    """

    def __init__(
        self,
        kind: SourceKind,
        fetcher: Fetcher,
        *,
        user_agent: str,
        max_sitemap_fetches: int = DEFAULT_MAX_SITEMAP_FETCHES,
        force: bool = False,
        entry_points: Sequence[str] = (),
    ) -> None:
        """Cria a fonte.

        Args:
            kind: ``SITEMAP`` (gratuito) ou ``SITEMAP_PAID``.
            fetcher: Porta HTTP com o intervalo por host.
            user_agent: Nome do nosso agente (regras do robots.txt).
            max_sitemap_fetches: Orçamento de sitemaps por site.
            force: Para a fonte paga, tenta mesmo sem bloqueio anterior na cascata.
            entry_points: Sitemaps por onde começar, no lugar dos declarados no robots.txt (a
                estratégia escolhida para a fonte); vazio explora a partir do robots.txt.
        """
        self.kind = kind
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._budget = max_sitemap_fetches
        self._force = force
        self._entry_points = tuple(entry_points)

    def applies(self, previous: Sequence[SourceFind]) -> bool:
        """A gratuita sempre; a paga só após bloqueio do sitemap gratuito (ou se forçada)."""
        if self.kind is not SourceKind.SITEMAP_PAID or self._force:
            return True
        return any(
            find.kind is SourceKind.SITEMAP and find.status is ProbeStatus.BLOCKED
            for find in previous
        )

    def find(self, site: Site, *, skip_child: SkipChild | None = None) -> SourceFind:
        """Percorre os sitemaps do site dentro do orçamento."""
        exploration = explore_site(
            site,
            self._fetcher,
            user_agent=self._user_agent,
            max_sitemap_fetches=self._budget,
            skip_child=skip_child,
            entry_points=self._entry_points,
        )
        if exploration.status is not ProbeStatus.OK:
            return SourceFind(
                self.kind,
                exploration.status,
                exploration.detail,
                (),
                fetched=exploration.fetched,
                skipped=exploration.skipped,
            )
        return SourceFind(
            self.kind,
            ProbeStatus.OK,
            "",
            collect_sitemap_urls(site, exploration.sitemaps),
            sitemaps=exploration.sitemaps,
            fetched=exploration.fetched,
            skipped=exploration.skipped,
            failed=exploration.failed,
            failure_detail=exploration.failure_detail,
        )


class FeedSource:
    """Feed RSS/Atom do site, lido de graça."""

    kind = SourceKind.FEED

    def __init__(self, fetcher: Fetcher, *, user_agent: str, feed_urls: Sequence[str] = ()) -> None:
        """Cria a fonte.

        Args:
            fetcher: Porta HTTP gratuita com o intervalo por host.
            user_agent: Nome do nosso agente (regras do robots.txt).
            feed_urls: Feeds escolhidos para a fonte (a estratégia): lidos direto; vazio deixa a
                home declarar os feeds ou adivinha os caminhos usuais.
        """
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._feed_urls = tuple(feed_urls)

    def applies(self, previous: Sequence[SourceFind]) -> bool:
        """O feed é tentado sempre que a cascata chega até ele."""
        return True

    def find(self, site: Site, *, skip_child: SkipChild | None = None) -> SourceFind:
        """Lê os feeds do site e transforma os itens em URLs descobertas."""
        exploration = explore_feed(
            site, self._fetcher, user_agent=self._user_agent, feed_urls=self._feed_urls
        )
        if exploration.status is not ProbeStatus.OK:
            return SourceFind(
                self.kind,
                exploration.status,
                exploration.detail,
                (),
                fetched=exploration.fetched,
            )
        origin = f"feed:{site.key}"
        urls = tuple(
            DiscoveredUrl.from_feed_entry(entry, site_key=site.key, source=origin)
            for entry in exploration.entries
        )
        return SourceFind(self.kind, ProbeStatus.OK, "", urls, fetched=exploration.fetched)


@dataclass(frozen=True, slots=True)
class CascadeResult:
    """Desfecho da cascata para um site.

    Attributes:
        chosen: A primeira fonte que serviu, ou ``None`` se nenhuma serviu.
        attempts: Tudo o que foi tentado, na ordem.
    """

    chosen: SourceFind | None
    attempts: tuple[SourceFind, ...]

    @property
    def status(self) -> ProbeStatus:
        """``OK`` se alguma fonte serviu; senão a falha mais informativa entre as tentativas."""
        if self.chosen is not None:
            return ProbeStatus.OK
        if not self.attempts:
            return ProbeStatus.NO_SITEMAP
        return min((a.status for a in self.attempts), key=_FAILURE_RANK.__getitem__)

    @property
    def detail(self) -> str:
        """Os motivos de cada tentativa (vazio quando alguma fonte serviu)."""
        if self.chosen is not None:
            return ""
        if len(self.attempts) == 1:
            return self.attempts[0].detail
        return "; ".join(f"{a.kind.value}: {a.detail}" for a in self.attempts)


def run_cascade(
    site: Site, sources: Sequence[DiscoverySource], *, skip_child: SkipChild | None = None
) -> CascadeResult:
    """Tenta as fontes em ordem e para na primeira que serve.

    Args:
        site: Fonte a explorar.
        sources: Fontes, da mais barata para a mais cara.
        skip_child: Dispensa sitemaps filhos inalterados (repassado às fontes de sitemap).

    Returns:
        O desfecho, com o que foi tentado.
    """
    attempts: list[SourceFind] = []
    for source in sources:
        if not source.applies(attempts):
            continue
        find = source.find(site, skip_child=skip_child)
        attempts.append(find)
        if find.ok:
            return CascadeResult(find, tuple(attempts))
    return CascadeResult(None, tuple(attempts))


def cascade_for(
    site: Site,
    *,
    free: Fetcher,
    paid: Fetcher | None,
    user_agent: str,
    known_blocked: bool,
    max_sitemap_fetches: int = DEFAULT_MAX_SITEMAP_FETCHES,
    use_feed: bool = True,
    strategies: Sequence[CandidateReport] = (),
    now: datetime | None = None,
) -> list[DiscoverySource]:
    """Monta a cascata de um site.

    As estratégias escolhidas para a fonte (``probe-sources``: principal e reserva) vêm primeiro,
    cada uma lendo o endereço que a sondagem mostrou servir. A cascata de sempre fica depois, de
    plano B: se o endereço escolhido mudou ou quebrou, a fonte não fica sem descoberta.

    Um host que já bloqueou o acesso gratuito não tem o sitemap gratuito tentado de novo (se há
    Bright Data para o resgatar): vai direto ao feed gratuito e, se ele não servir, ao sitemap pago.

    Args:
        site: Fonte a explorar.
        free: Fetcher gratuito.
        paid: Fetcher pago (Bright Data), ou ``None`` se não está habilitado.
        user_agent: Nome do nosso agente.
        known_blocked: Se o host já bloqueou o acesso gratuito em rodadas anteriores.
        max_sitemap_fetches: Orçamento de sitemaps por site.
        use_feed: Se o feed entra na cascata (também o da estratégia).
        strategies: Estratégias escolhidas da fonte, a principal antes da reserva. Um endereço com
            modelo de data (``{YYYY}-{MM}-{DD}``) é lido para hoje e para ontem.
        now: Instante de referência para os modelos de data (padrão: agora).

    Returns:
        As fontes, na ordem em que serão tentadas.
    """
    skip_free_sitemap = known_blocked and paid is not None
    sources: list[DiscoverySource] = []
    moment = now or datetime.now(UTC)
    for chosen in strategies:
        if chosen.kind is StrategyKind.FEED:
            if use_feed:
                sources.append(
                    FeedSource(
                        free,
                        user_agent=user_agent,
                        feed_urls=render_window(chosen.url, moment),
                    )
                )
        elif chosen.kind is not None and not skip_free_sitemap:
            sources.append(
                SitemapSource(
                    SourceKind.SITEMAP,
                    free,
                    user_agent=user_agent,
                    max_sitemap_fetches=max_sitemap_fetches,
                    entry_points=render_window(chosen.url, moment),
                )
            )
    if not skip_free_sitemap:
        sources.append(
            SitemapSource(
                SourceKind.SITEMAP,
                free,
                user_agent=user_agent,
                max_sitemap_fetches=max_sitemap_fetches,
            )
        )
    if use_feed:
        sources.append(FeedSource(free, user_agent=user_agent))
    if paid is not None:
        sources.append(
            SitemapSource(
                SourceKind.SITEMAP_PAID,
                paid,
                user_agent=user_agent,
                max_sitemap_fetches=max_sitemap_fetches,
                force=known_blocked,
            )
        )
    return sources


def collect_sitemap_urls(
    site: Site, sitemaps: Sequence[ExploredSitemap]
) -> tuple[DiscoveredUrl, ...]:
    """URLs da fonte nos sitemaps lidos, sem repetição (variantes de uma URL contam uma vez)."""
    unique: dict[str, DiscoveredUrl] = {}
    for item in sitemaps:
        for entry in item.sitemap.entries:
            if site.owns(entry.loc):
                found = DiscoveredUrl.from_entry(entry, site_key=site.key, source_sitemap=item.url)
                unique.setdefault(found.url_hash, found)
    return tuple(unique.values())
