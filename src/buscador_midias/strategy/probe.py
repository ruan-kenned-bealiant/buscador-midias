"""Sondagem de uma fonte: gera candidatos em etapas, avalia, pontua e escolhe."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from buscador_midias.discovery.explore import (
    Exploration,
    ProbeStatus,
    is_taxonomy_sitemap,
    origin_of,
    rank_urls,
    read_robots,
    section_terms,
)
from buscador_midias.discovery.feed_explore import GUESSED_FEED_PATHS, MAX_DECLARED_FEEDS
from buscador_midias.discovery.feeds import find_feed_links
from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.domain.dated_url import render, templatize
from buscador_midias.domain.site import Site
from buscador_midias.domain.strategy import (
    CandidateOrigin,
    CandidateReport,
    Role,
    StrategyKind,
    Verdict,
)
from buscador_midias.net.fetcher import Fetcher
from buscador_midias.strategy.evaluate import DEFAULT_MAX_CHILDREN, evaluate_candidate
from buscador_midias.strategy.manual import is_excluded
from buscador_midias.strategy.scoring import ScoringPolicy, choose, plan_coverage

DEFAULT_MAX_REQUESTS = 40
MAX_ROBOTS_SITEMAPS = 6
FALLBACK_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")
NEWS_SITEMAP_PATHS = (
    "/news-sitemap.xml",
    "/sitemap-news.xml",
    "/news_sitemap.xml",
    "/sitemap_news.xml",
)


@dataclass(frozen=True, slots=True)
class SiteStrategy:
    """O resultado da sondagem de uma fonte.

    Attributes:
        site_key: A fonte.
        status: ``OK`` se há uma estratégia escolhida; senão o motivo (bloqueio, robots, nada que
            sirva).
        detail: Explicação curta da falha (vazio quando ``OK``).
        reports: Todos os candidatos sondados, na ordem, com o papel dos escolhidos.
        requests: Requisições feitas (inclui o ``robots.txt`` e a home).
    """

    site_key: str
    status: ProbeStatus
    detail: str
    reports: tuple[CandidateReport, ...]
    requests: int

    @property
    def primary(self) -> CandidateReport | None:
        """A estratégia que a rodada usa."""
        return next((r for r in self.reports if r.role is Role.PRIMARY), None)

    @property
    def backup(self) -> CandidateReport | None:
        """A estratégia que cobre a falha da principal."""
        return next((r for r in self.reports if r.role is Role.BACKUP), None)

    @property
    def complements(self) -> tuple[CandidateReport, ...]:
        """As fontes que acrescentam o que o principal e a reserva não listam."""
        return tuple(r for r in self.reports if r.role is Role.COMPLEMENT)


class _Run:
    """O estado de uma sondagem em andamento: o que já se tentou e o orçamento gasto."""

    def __init__(
        self,
        site: Site,
        fetcher: Fetcher,
        robots: RobotsPolicy,
        *,
        user_agent: str,
        now: datetime,
        scoring: ScoringPolicy,
        max_requests: int,
        spent: int,
        exclude: Sequence[str] = (),
    ) -> None:
        self.site = site
        self.fetcher = fetcher
        self.robots = robots
        self.user_agent = user_agent
        self.now = now
        self.scoring = scoring
        self.max_requests = max_requests
        self.requests = spent
        self.reports: list[CandidateReport] = []
        self._tried: set[str] = set()
        self._exclude = tuple(exclude)

    def has_usable(self, kind: StrategyKind) -> bool:
        """Se algum candidato do tipo já serve."""
        return any(
            r.kind is kind and r.score > 0 and r.verdict is not Verdict.SLOW for r in self.reports
        )

    def evaluate(
        self,
        urls: Sequence[str],
        origin: CandidateOrigin,
        *,
        until_usable: bool = False,
        forced: bool = False,
    ) -> None:
        """Avalia as URLs ainda não tentadas, enquanto houver orçamento."""
        for raw in urls:
            # Um sitemap "do dia" (``.../2026-10-15/``) vira modelo: o que se grava não envelhece.
            url = templatize(raw, self.now)
            if url in self._tried or self.requests >= self.max_requests:
                continue
            self._tried.add(url)
            if self._is_excluded(raw) or self._is_excluded(url):
                self.reports.append(_excluded(url, origin))
                continue
            report = self._evaluate_once(url, origin, forced)
            # Um endereço que o site mesmo declarou e que falhou merece outra tentativa: 404 e
            # erros costumam ser de CDN instável (os adivinhados falham de verdade com 404).
            declared = origin in (CandidateOrigin.ROBOTS, CandidateOrigin.HINT)
            if (
                declared
                and report.verdict is Verdict.UNAVAILABLE
                and self.requests + report.requests < self.max_requests
            ):
                again = self._evaluate_once(url, origin, forced)
                report = replace(again, requests=report.requests + again.requests)
            self.requests += report.requests
            self.reports.append(replace(report, url=url, forced=forced))
            if until_usable and report.score > 0 and report.verdict is not Verdict.SLOW:
                break

    def _evaluate_once(self, url: str, origin: CandidateOrigin, forced: bool) -> CandidateReport:
        """Avalia o endereço (de hoje, se for modelo) uma vez."""
        return evaluate_candidate(
            render(url, self.now),
            origin,
            site=self.site,
            fetcher=self.fetcher,
            robots=self.robots,
            user_agent=self.user_agent,
            now=self.now,
            scoring=self.scoring,
            max_children=DEFAULT_MAX_CHILDREN,
            allow_listing=forced,
            exclude=self._exclude,
        )

    def _is_excluded(self, url: str) -> bool:
        """Se uma regra manual manda não sondar o endereço."""
        return is_excluded(url, self._exclude)


def _excluded(url: str, origin: CandidateOrigin) -> CandidateReport:
    """O relatório de um endereço que uma regra manual excluiu: não se baixa nada."""
    return CandidateReport(
        url, origin, Verdict.INVALID, None, None, 0.0, "excluído à mão (estrategias_manuais.csv)"
    )


def probe_site(
    site: Site,
    fetcher: Fetcher,
    *,
    user_agent: str,
    now: datetime,
    hints: Sequence[str] = (),
    scoring: ScoringPolicy | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    exclude: Sequence[str] = (),
    forced: Sequence[str] = (),
) -> SiteStrategy:
    """Descobre como obter as matérias novas da fonte e escolhe a melhor estratégia.

    Em etapas, da mais barata para a mais cara, e só avançando quando falta algo:

    1. os endereços forçados à mão, as dicas (endereços já conhecidos, ex.: de um levantamento), o
       que o ``robots.txt`` declara (sem sitemaps de tags e produtos) ou, sem declaração,
       ``/sitemap.xml``;
    2. se nenhum sitemap de notícias serviu, os nomes usuais de sitemap de notícias;
    3. se nenhum feed serviu, os feeds que a home declara ou, sem declaração, os caminhos usuais.

    Cada candidato é baixado, medido (quantas matérias recentes, com título, com data) e julgado; a
    nota escolhe o principal e, de outro tipo de documento, a reserva. A sondagem respeita o
    ``robots.txt`` e o orçamento de requisições, e nunca levanta por causa do site.

    Args:
        site: A fonte.
        fetcher: Porta HTTP (com o intervalo por host, em produção).
        user_agent: Nome do nosso agente.
        now: Instante de referência.
        hints: Endereços candidatos conhecidos de antemão (sondados primeiro).
        scoring: Parâmetros do julgamento.
        max_requests: Orçamento de requisições (inclui o ``robots.txt`` e a home).
        exclude: Endereços que uma regra manual manda não sondar (``*`` no fim vale o prefixo).
        forced: Endereços que uma regra manual manda usar como principal (se servirem).

    Returns:
        A estratégia escolhida e todos os candidatos com o que se concluiu de cada um.
    """
    policy = read_robots(site, fetcher, user_agent)
    if isinstance(policy, Exploration):
        return SiteStrategy(site.key, policy.status, policy.detail, (), 1)

    run = _Run(
        site,
        fetcher,
        policy,
        user_agent=user_agent,
        now=now,
        scoring=scoring or ScoringPolicy(),
        max_requests=max_requests,
        spent=1,
        exclude=exclude,
    )
    origin = origin_of(site.url)
    run.evaluate(forced, CandidateOrigin.HINT, forced=True)
    run.evaluate(hints, CandidateOrigin.HINT)
    # Sitemaps de tags e produtos não ocupam vaga: se só há deles, vale o caminho usual.
    listed = tuple(u for u in policy.sitemaps if not is_taxonomy_sitemap(u))
    declared = rank_urls(listed[:MAX_ROBOTS_SITEMAPS], section_terms(site))
    run.evaluate(declared, CandidateOrigin.ROBOTS)
    if not declared:
        run.evaluate([f"{origin}{p}" for p in FALLBACK_SITEMAP_PATHS], CandidateOrigin.COMMON_PATH)
    if not run.has_usable(StrategyKind.NEWS_SITEMAP):
        run.evaluate([f"{origin}{p}" for p in NEWS_SITEMAP_PATHS], CandidateOrigin.COMMON_PATH)
    if not run.has_usable(StrategyKind.FEED):
        _probe_feeds(run, origin)

    reports = plan_coverage(choose(run.reports), run.scoring)
    chosen = any(r.role is Role.PRIMARY for r in reports)
    status, detail = (ProbeStatus.OK, "") if chosen else _failure(reports)
    return SiteStrategy(site.key, status, detail, reports, run.requests)


def _probe_feeds(run: _Run, origin: str) -> None:
    """Sonda os feeds declarados pela home ou, sem declaração, os caminhos usuais."""
    declared: tuple[str, ...] = ()
    if run.requests < run.max_requests and run.robots.allows(run.site.url, run.user_agent):
        run.requests += 1
        home = run.fetcher.get(run.site.url)
        if home.ok:
            found = find_feed_links(home.content, home.final_url or run.site.url)
            declared = found[:MAX_DECLARED_FEEDS]
    if declared:
        run.evaluate(declared, CandidateOrigin.HOME_LINK)
    else:
        guesses = [f"{origin}{path}" for path in GUESSED_FEED_PATHS]
        run.evaluate(guesses, CandidateOrigin.COMMON_PATH, until_usable=True)


def _failure(reports: Sequence[CandidateReport]) -> tuple[ProbeStatus, str]:
    """O motivo de não haver estratégia: bloqueio se algo bloqueou; senão, nada que sirva."""
    counts = Counter(r.verdict.value for r in reports)
    detail = ", ".join(f"{name} {n}" for name, n in sorted(counts.items())) or "sem candidatos"
    if counts[Verdict.BLOCKED.value]:
        return ProbeStatus.BLOCKED, detail
    return ProbeStatus.NO_SITEMAP, detail
