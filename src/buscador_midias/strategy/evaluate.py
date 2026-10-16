"""Avalia um endereço candidato: baixa, lê (sitemap, índice ou feed), mede e julga."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from buscador_midias.discovery.explore import (
    BLOCKING_STATUSES,
    is_taxonomy_sitemap,
    prioritize_children,
    section_terms,
)
from buscador_midias.discovery.feeds import parse_feed, parse_feed_entries
from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.discovery.sitemap_parser import SitemapParseError, parse_sitemap
from buscador_midias.domain.site import Site
from buscador_midias.domain.sitemap import ParsedSitemap, SitemapKind
from buscador_midias.domain.strategy import (
    CandidateOrigin,
    CandidateReport,
    Item,
    Measure,
    StrategyKind,
    Verdict,
)
from buscador_midias.net.fetcher import Fetcher, FetchResult
from buscador_midias.strategy.measure import measure_items
from buscador_midias.strategy.scoring import ScoringPolicy, judge, score

DEFAULT_MAX_CHILDREN = 3
# Do mais ao menos informativo, para escolher o motivo quando todos os filhos de um índice falham.
_FAILURE_ORDER = (Verdict.BLOCKED, Verdict.UNAVAILABLE, Verdict.INVALID, Verdict.DISALLOWED)


@dataclass(frozen=True, slots=True)
class _Failure:
    """Um documento que não pôde ser lido."""

    verdict: Verdict
    detail: str


@dataclass(frozen=True, slots=True)
class _Document:
    """Um documento lido: as matérias e o tipo."""

    kind: StrategyKind
    items: tuple[Item, ...]
    parsed: ParsedSitemap | None
    size_bytes: int


def evaluate_candidate(
    url: str,
    origin: CandidateOrigin,
    *,
    site: Site,
    fetcher: Fetcher,
    robots: RobotsPolicy,
    user_agent: str,
    now: datetime,
    scoring: ScoringPolicy | None = None,
    max_children: int = DEFAULT_MAX_CHILDREN,
) -> CandidateReport:
    """Sonda um candidato e devolve o relatório (nunca levanta por causa do site).

    Um índice de sitemaps é avaliado pelos seus melhores filhos (os que citam a seção da fonte, os
    de notícias e os de ``lastmod`` mais recente; listagens de tags e autores ficam de fora): o que
    importa do candidato é o que o índice entrega, e é o índice que se lembra, porque o endereço
    dos filhos costuma mudar (um por dia ou por mês).

    Args:
        url: Endereço a sondar.
        origin: De onde veio o endereço.
        site: A fonte (define o que são URLs "dela").
        fetcher: Porta HTTP (com o intervalo por host, em produção).
        robots: Política do robots.txt da fonte.
        user_agent: Nome do nosso agente.
        now: Instante de referência.
        scoring: Parâmetros do julgamento.
        max_children: Quantos filhos de um índice se leem.

    Returns:
        O relatório, com veredito, medida e nota.
    """
    scoring = scoring or ScoringPolicy()
    requests = 0

    def report(
        verdict: Verdict,
        detail: str,
        kind: StrategyKind | None = None,
        measure: Measure | None = None,
        value: float = 0.0,
    ) -> CandidateReport:
        return CandidateReport(
            url, origin, verdict, kind, measure, value, detail, requests=requests
        )

    if not robots.allows(url, user_agent):
        return report(Verdict.DISALLOWED, "robots.txt proíbe o acesso")
    requests = 1
    result = fetcher.get(url)
    if (failure := _failure(result)) is not None:
        return report(failure.verdict, failure.detail)

    document = _read(result, site)
    if isinstance(document, _Failure):
        return report(document.verdict, document.detail)
    if document.parsed is not None and document.parsed.kind is SitemapKind.INDEX:
        children = _children(document.parsed, site, robots, user_agent, max_children)
        documents: list[_Document] = []
        failures: list[_Failure] = []
        for child in children:
            requests += 1
            outcome = _fetch_urlset(fetcher, child, site)
            if isinstance(outcome, _Failure):
                failures.append(outcome)
            else:
                documents.append(outcome)
        if not documents:
            worst = min(
                failures or [_Failure(Verdict.INVALID, "índice sem sitemaps de matérias")],
                key=lambda f: _FAILURE_ORDER.index(f.verdict),
            )
            return report(worst.verdict, f"índice: {worst.detail}")
        kind = (
            StrategyKind.NEWS_SITEMAP
            if any(d.kind is StrategyKind.NEWS_SITEMAP for d in documents)
            else StrategyKind.SITEMAP
        )
        items = tuple(item for d in documents for item in d.items)
        size = _size(result) + sum(d.size_bytes for d in documents)
        detail = f"índice: {len(documents)} de {len(document.parsed.children)} filhos lidos"
    else:
        kind, items, size = document.kind, document.items, document.size_bytes
        detail = ""

    measure = measure_items(kind, items, now=now, size_bytes=size)
    verdict = judge(measure, scoring)
    if verdict is Verdict.EMPTY and not detail:
        detail = "nenhuma URL da fonte"
    return report(verdict, detail, kind, measure, score(measure, verdict, scoring))


def _size(result: FetchResult) -> int:
    """Bytes do corpo de uma resposta."""
    return len(result.content)


def _failure(result: FetchResult) -> _Failure | None:
    """Traduz uma resposta que não é utilizável em veredito e motivo."""
    if result.ok:
        return None
    if result.status in BLOCKING_STATUSES:
        return _Failure(Verdict.BLOCKED, f"HTTP {result.status}")
    return _Failure(Verdict.UNAVAILABLE, result.error or f"HTTP {result.status}")


def _read(result: FetchResult, site: Site) -> _Document | _Failure:
    """Interpreta o corpo como sitemap (urlset ou índice) ou, se não for, como feed."""
    try:
        parsed = parse_sitemap(result.content)
    except SitemapParseError as error:
        feed = _read_feed(result, site)
        return feed if feed is not None else _Failure(Verdict.INVALID, str(error))
    if parsed.kind is SitemapKind.INDEX:
        return _Document(StrategyKind.SITEMAP, (), parsed, _size(result))
    return _from_urlset(parsed, site, _size(result))


def _from_urlset(parsed: ParsedSitemap, site: Site, size: int) -> _Document:
    """Matérias da fonte em um ``urlset`` (URLs de outros domínios não contam)."""
    items = tuple(
        Item(entry.loc, entry.timestamp, entry.title)
        for entry in parsed.entries
        if site.owns(entry.loc)
    )
    kind = StrategyKind.NEWS_SITEMAP if parsed.has_news else StrategyKind.SITEMAP
    return _Document(kind, items, parsed, size)


def _read_feed(result: FetchResult, site: Site) -> _Document | None:
    """Interpreta o corpo como feed; ``None`` se não for um."""
    if parse_feed(result.content) is None:
        return None
    entries = parse_feed_entries(result.content, result.final_url or result.url)
    items = tuple(Item(e.url, e.published, e.title) for e in entries if site.owns(e.url))
    return _Document(StrategyKind.FEED, items, None, _size(result))


def _children(
    index: ParsedSitemap, site: Site, robots: RobotsPolicy, user_agent: str, limit: int
) -> list[str]:
    """Filhos de um índice que valem a leitura: sem listagens e dentro do que o robots permite."""
    ranked = prioritize_children(index.children, section_terms(site))
    candidates = [c for c in ranked if not is_taxonomy_sitemap(c.loc)] or ranked
    return [c.loc for c in candidates[:limit] if robots.allows(c.loc, user_agent)]


def _fetch_urlset(fetcher: Fetcher, url: str, site: Site) -> _Document | _Failure:
    """Baixa um filho de índice, que precisa ser um ``urlset`` (índice de índices não se segue)."""
    result = fetcher.get(url)
    if (failure := _failure(result)) is not None:
        return failure
    document = _read(result, site)
    if isinstance(document, _Failure):
        return document
    if document.parsed is None or document.parsed.kind is SitemapKind.INDEX:
        return _Failure(Verdict.INVALID, f"{url}: não é um sitemap de matérias")
    return document
