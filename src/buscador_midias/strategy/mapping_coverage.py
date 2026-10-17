"""Leitura dos mecanismos e comparação de suas URLs com amostras das seções."""

import re
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.domain.dated_url import render_window
from buscador_midias.domain.mapping import (
    MapPage,
    MapSource,
    PageStatus,
    SectionCoverage,
    SourceContribution,
)
from buscador_midias.domain.site import Site
from buscador_midias.domain.strategy import CandidateOrigin, CandidateReport, StrategyKind
from buscador_midias.domain.url import canonicalize_url
from buscador_midias.net.fetcher import Fetcher, FetchResult
from buscador_midias.strategy.evaluate import evaluate_candidate
from buscador_midias.strategy.manual import is_excluded
from buscador_midias.strategy.mapping_net import (
    ORIGIN_FAILURE_LIMIT,
    MappingFetcher,
    is_transport_failure,
)
from buscador_midias.strategy.section_sources import find_feed_sources

_READABLE = frozenset({"fresh", "undated", "slow", "stale", "archive", "empty"})
_LISTING_PAGINATION = re.compile(
    r"/index/feed/pagina-\d+(?:\.(?:ghtml|html?|shtml))?/?$", re.IGNORECASE
)
_AUDIT_PAUSED = "auditoria suspensa após"


def audit_paused(checks: Sequence[MapSource]) -> bool:
    """A passagem parou por falhas de conexão, mantendo mecanismos ainda não tentados."""
    return any(_AUDIT_PAUSED in check.detail for check in checks)


def is_html_pagination(check: MapSource) -> bool:
    """Paginação HTML já observada, cujo nome feed não comprova RSS/Atom."""
    return check.kind in ("feed_index", "html_pagination") and bool(
        _LISTING_PAGINATION.search(urlsplit(check.url).path)
    )


def normalize_source_audits(checks: Sequence[MapSource]) -> list[MapSource]:
    """Corrige índices HTML antigos sem perder URLs observadas, datas ou custos."""
    return [
        replace(
            check, verdict="invalid", kind="html_pagination",
            detail="paginação HTML de listagem; não é índice de mecanismos",
        ) if check.kind == "feed_index" and is_html_pagination(check) else replace(
            check, verdict="invalid", kind="comments_feed",
            detail="feed de comentários; não comprova matérias da seção",
        ) if check.kind == "feed" and check.listed_urls and all(
            _comment_reference(url) for url in check.listed_urls
        ) else check
        for check in checks
    ]


def _comment_reference(url: str) -> bool:
    parts = urlsplit(url)
    return bool(
        re.fullmatch(r"comment-\d+", parts.fragment, re.IGNORECASE)
        or "replytocom" in parse_qs(parts.query)
    )


def plan_source_audit(
    site: Site,
    pages: Sequence[MapPage],
    checks: Sequence[MapSource],
    candidates: Sequence[CandidateReport],
    *,
    now: datetime,
    hints: Sequence[str] = (),
    forced: Sequence[str] = (),
    retry_failed: bool = False,
) -> list[str]:
    """Prioriza mecanismos ainda não lidos e retoma os interrompidos por orçamento.

    Leituras atuais são reaproveitadas por até 24 h; amostras mais novas invalidam esse
    reaproveitamento. Índices HTML já lidos alimentam a fila com os mecanismos que apontam.
    """
    urls = list(forced)
    urls += [u for p in pages if p.is_section_candidate for u in p.feeds + p.feed_indexes]
    urls += [
        u for check in checks if check.kind == "feed_index" and not is_html_pagination(check)
        for u in check.listed_urls
    ]
    saved = sorted(
        (r for r in candidates if r.kind not in (None, StrategyKind.LISTING) and r.score > 0),
        key=lambda r: (r.role is None, -r.score),
    )
    urls += [r.url for r in saved]
    urls += list(hints)
    expanded = dict.fromkeys(
        u for raw in urls for u in render_window(raw, now) if site.owns_host(u)
    )
    previous = {c.url: c for c in normalize_source_audits(checks)}
    newest = max(
        (
            p.checked_at
            for p in pages
            if p.checked_at
            and p.status is PageStatus.OK
            and p.article_urls
            and p.is_section_candidate
        ),
        default=now - timedelta(days=1),
    )
    threshold = max(newest, now - timedelta(days=1))
    queue = [
        u
        for u in expanded
        if u not in previous or (
            not is_html_pagination(previous[u]) and previous[u].kind != "comments_feed" and (
                previous[u].checked_at < threshold
                or "orçamento" in previous[u].detail
                or (retry_failed and previous[u].verdict not in _READABLE | {"feed_index"})
            )
        )
    ]
    queue.sort(
        key=lambda u: (
            0 if u not in previous else 1 if "orçamento" in previous[u].detail else 2,
            previous[u].checked_at if u in previous else now,
        )
    )
    return queue


class _ObservedFetcher:
    """Mantém somente a resposta de entrada durante sua avaliação."""

    def __init__(self, inner: Fetcher, url: str) -> None:
        self.inner = inner
        self.url = url
        self.response: FetchResult | None = None

    def get(self, url: str) -> FetchResult:
        result = self.inner.get(url)
        if url == self.url:
            self.response = result
        return result


def audit_sources(
    site: Site,
    fetcher: Fetcher,
    urls: Sequence[str],
    *,
    user_agent: str,
    now: datetime,
    max_requests: int = 20,
    exclude: Sequence[str] = (),
    on_source: Callable[[MapSource], None] | None = None,
) -> list[MapSource]:
    """Lê cada mecanismo uma vez, com orçamento incluindo robots e filhos de índices.

    Não baixa matérias. Respeita exclusões manuais também nos filhos. O orçamento deixa
    candidatos não tentados para o próximo lote; uma leitura parcial fica identificada.
    """
    if max_requests < 1:
        raise ValueError("max_requests precisa ser positivo")
    guarded = MappingFetcher(fetcher, site, user_agent, max_requests)
    checks: list[MapSource] = []
    queue = deque(dict.fromkeys(urls))
    seen: set[str] = set()
    failures: dict[str, int] = {}
    while queue:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        if not site.owns_host(url) or is_excluded(url, exclude):
            continue
        if guarded.requests >= max_requests:
            break
        before = guarded.requests
        observed = _ObservedFetcher(guarded, url)
        report = evaluate_candidate(
            url,
            CandidateOrigin.HOME_LINK,
            site=site,
            fetcher=observed,
            robots=RobotsPolicy.allow_all(),
            user_agent=user_agent,
            now=now,
            exclude=exclude,
            include_listed_urls=True,
        )
        verdict = report.verdict.value
        if "robots.txt proíbe" in report.detail:
            verdict = "disallowed"
        counts = re.search(r"índice: (\d+) de (\d+)", report.detail)
        partial = guarded.exhausted or bool(counts and int(counts[1]) < int(counts[2]))
        detail = report.detail
        kind = report.kind.value if report.kind else None
        listed = tuple(sorted(report.listed_urls))
        response = observed.response
        pagination = (
            verdict == "invalid" and response is not None and response.ok
            and bool(_LISTING_PAGINATION.search(urlsplit(url).path))
            and (
                "html" in (response.content_type or "").lower()
                or bool(re.search(
                    rb"<(?:!doctype\s+html|html|body|div|section|article)\b",
                    response.content[:1024], re.IGNORECASE,
                ))
            )
        )
        if pagination:
            kind = "html_pagination"
            detail = "paginação HTML de listagem; não é índice de mecanismos"
        elif verdict == "invalid" and response is not None and response.ok:
            found = find_feed_sources(response.content, response.final_url or url, site)
            discovered = tuple(dict.fromkeys(found.declared + found.direct + found.indexes))
            discovered = tuple(u for u in discovered if u != url and not is_excluded(u, exclude))
            if discovered:
                kind = verdict = "feed_index"
                listed = discovered
                queue.extend(discovered)
                detail = "página HTML aponta mecanismos; cada endereço exige sua própria leitura"
        if guarded.exhausted and "orçamento" not in detail:
            detail += "; orçamento de mapeamento esgotado"
        origin = urlsplit(url).netloc
        failures[origin] = (
            failures.get(origin, 0) + 1
            if response is not None and is_transport_failure(response) else 0
        )
        paused = failures[origin] >= ORIGIN_FAILURE_LIMIT
        if paused:
            detail += (
                f"; {_AUDIT_PAUSED} {ORIGIN_FAILURE_LIMIT} falhas consecutivas "
                f"de conexão em {origin}; mecanismos não tentados preservados"
            )
        check = MapSource(
            url=url,
            verdict=verdict,
            kind=kind,
            listed_urls=listed,
            checked_at=now,
            requests=guarded.requests - before,
            partial=partial,
            detail=detail,
        )
        check = normalize_source_audits([check])[0]
        checks.append(check)
        if on_source is not None:
            on_source(check)
        if paused:
            break
    return checks


def compare_page(page: MapPage, checks: Sequence[MapSource]) -> SectionCoverage:
    """Confere a amostra contra leituras feitas depois da página, com URLs canônicas.

    ``sample_full`` só diz que os links observados aparecem nos mecanismos medidos.
    Uma seção sem links reconhecidos, ainda não lida ou com leituras antigas fica pendente.
    """
    return CoverageIndex(checks).compare(page)


class CoverageIndex:
    """Reaproveita URLs canônicas dos mecanismos entre as seções da mesma fonte."""

    def __init__(self, checks: Sequence[MapSource]) -> None:
        """Indexa uma fotografia das leituras, sem executar novas requisições."""
        self.checks = tuple(normalize_source_audits(checks))
        self.listed = {
            s.url: frozenset(
                canonicalize_url(u) for u in s.listed_urls if not _comment_reference(u)
            )
            for s in self.checks
            if s.verdict in _READABLE and s.kind is not None
        }

    def compare(self, page: MapPage) -> SectionCoverage:
        """Sugere complementos pelo ganho observado, sem garantir um conjunto mínimo."""
        if not page.is_section_candidate:
            return SectionCoverage("not_section", 0, 0)
        if page.status is not PageStatus.OK:
            return SectionCoverage("not_read", 0, 0)
        sample = {canonicalize_url(url): url for url in page.article_urls}
        if not sample:
            return SectionCoverage("no_sample", 0, 0)
        current = [s for s in self.checks if page.checked_at and s.checked_at >= page.checked_at]
        if not current:
            return SectionCoverage("pending", len(sample), 0, missing=tuple(sample.values()))
        readable = [s for s in current if s.verdict in _READABLE and s.kind is not None]
        if not readable:
            if any(s.kind == "feed_index" or "orçamento" in s.detail for s in current):
                return SectionCoverage("pending", len(sample), 0, missing=tuple(sample.values()))
            return SectionCoverage("source_failed", len(sample), 0, missing=tuple(sample.values()))
        matches = [(s, set(sample).intersection(self.listed[s.url])) for s in readable]
        matches = [(s, found) for s, found in matches if found]
        covered = set().union(*(found for _, found in matches))
        missing = tuple(url for key, url in sample.items() if key not in covered)
        status = "sample_full" if not missing else "partial" if covered else "no_match"
        remaining = set(covered)
        suggested: list[SourceContribution] = []
        while remaining:
            source, found = min(
                matches,
                key=lambda pair: (
                    -len(pair[1] & remaining),
                    pair[0].partial,
                    pair[0].url,
                ),
            )
            added = len(found & remaining)
            suggested.append(SourceContribution(source, len(found), added))
            remaining.difference_update(found)
        return SectionCoverage(
            status,
            len(sample),
            len(covered),
            tuple(s.url for s, _ in matches),
            missing,
            tuple(suggested),
        )
