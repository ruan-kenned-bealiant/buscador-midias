"""Exploração do feed (RSS/Atom) de um site: a alternativa ao sitemap quando ele não serve."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from buscador_midias.discovery.explore import (
    BLOCKING_STATUSES,
    Exploration,
    ProbeStatus,
    origin_of,
    read_robots,
)
from buscador_midias.discovery.feeds import find_feed_links, parse_feed, parse_feed_entries
from buscador_midias.domain.feed import FeedEntry
from buscador_midias.domain.site import Site
from buscador_midias.net.fetcher import Fetcher

MAX_DECLARED_FEEDS = 3
GUESSED_FEED_PATHS = ("/feed/", "/rss.xml", "/rss")


@dataclass(frozen=True, slots=True)
class FeedExploration:
    """Resultado da leitura dos feeds de um site.

    Attributes:
        status: ``OK`` se algum feed trouxe matérias da fonte; senão o motivo da falha
            (``NO_SITEMAP`` quer dizer "nenhum feed encontrado").
        detail: Explicação curta da falha (vazio quando ``OK``).
        entries: Matérias da fonte, sem repetir, na ordem em que os feeds as listam.
        fetched: Quantas requisições foram feitas (home e feeds; sem contar o robots.txt).
    """

    status: ProbeStatus
    detail: str
    entries: tuple[FeedEntry, ...]
    fetched: int


def explore_feed(
    site: Site,
    fetcher: Fetcher,
    *,
    user_agent: str,
    feed_urls: Sequence[str] = (),
    since: datetime | None = None,
) -> FeedExploration:
    """Lê os feeds do site: os que a home declara (até três) ou, sem declaração, os caminhos usuais.

    Respeita o ``robots.txt``. Uma home que bloqueia não impede tentar os caminhos usuais: é comum
    o ``/feed/`` passar onde a home recusa. Só entram itens que pertencem à fonte.

    Args:
        site: Fonte a explorar.
        fetcher: Porta HTTP (use a versão com intervalo por host em produção).
        user_agent: Nome do nosso agente, usado nas regras do robots.txt.
        feed_urls: Feeds já escolhidos (a estratégia da fonte): são lidos direto, sem baixar a
            home nem adivinhar caminhos.
        since: Descarta itens datados anteriores à janela; preserva itens sem data para conferência.

    Returns:
        A exploração; nunca levanta por causa do conteúdo ou da rede do site.
    """
    policy = read_robots(site, fetcher, user_agent)
    if isinstance(policy, Exploration):
        return FeedExploration(policy.status, policy.detail, (), 0)

    fetched = 0
    blocked = False
    failure = ""
    declared: tuple[str, ...] = tuple(feed_urls)
    unreachable = False
    if not declared:
        fetched = 1
        home = fetcher.get(site.url)
        if home.ok:
            declared = find_feed_links(home.content, home.final_url or site.url)[
                :MAX_DECLARED_FEEDS
            ]
        else:
            blocked = home.status in BLOCKING_STATUSES
            failure = home.error or f"home: HTTP {home.status}"
            unreachable = home.status is None

    origin = origin_of(site.url)
    guessing = not declared
    candidates = declared or tuple(f"{origin}{path}" for path in GUESSED_FEED_PATHS)

    entries: dict[str, FeedEntry] = {}
    accessible_feed = False
    for url in candidates:
        if not policy.allows(url, user_agent):
            continue
        fetched += 1
        page = fetcher.get(url)
        if not page.ok:
            blocked = blocked or page.status in BLOCKING_STATUSES
            failure = failure or page.error or f"{url}: HTTP {page.status}"
            unreachable = unreachable or (bool(feed_urls) and page.status is None)
            continue
        summary = parse_feed(page.content)
        accessible_feed = accessible_feed or summary is not None
        found = [
            e
            for e in parse_feed_entries(page.content, url)
            if site.owns(e.url) and (since is None or e.published is None or e.published >= since)
        ]
        for entry in found:
            entries.setdefault(entry.url, entry)
        if found and guessing:
            break

    if entries:
        return FeedExploration(ProbeStatus.OK, "", tuple(entries.values()), fetched)
    if accessible_feed:
        return FeedExploration(
            ProbeStatus.OK,
            "feed acessível, sem notícias na janela",
            (),
            fetched,
        )
    if blocked:
        return FeedExploration(ProbeStatus.BLOCKED, failure or "feed bloqueado", (), fetched)
    if failure and unreachable:
        return FeedExploration(ProbeStatus.ERROR, failure, (), fetched)
    return FeedExploration(ProbeStatus.NO_SITEMAP, "nenhum feed encontrado", (), fetched)
