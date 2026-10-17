"""Varredura de listagens: da home às páginas de seção, atrás de links de matéria.

Para o site sem sitemap nem feed aproveitável a home e as páginas de seção são, por construção, as
listas das matérias mais recentes. Não há padrão único de URL entre os sites, mas há padrões
reconhecíveis: o título da matéria é um *slug* longo, ou o caminho traz data ou um número de
identificação; uma seção é um caminho curto de palavras (``/politica``, ``/ac/acre``). A varredura
parte da home, segue as páginas de seção e, de cada uma, colhe matérias e novas seções (a URL de
uma seção ajuda a achar as outras), até um teto de páginas por fonte. Roda para toda fonte, no
começo da sondagem: é a estrutura do site, contra a qual se mede o que o sitemap ou o feed escolhido
cobre.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlsplit

from buscador_midias.discovery.explore import BLOCKING_STATUSES
from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.discovery.url_filter import l0_filter
from buscador_midias.domain.site import Site
from buscador_midias.domain.strategy import (
    CandidateOrigin,
    CandidateReport,
    Item,
    StrategyKind,
    Verdict,
)
from buscador_midias.net.fetcher import Fetcher
from buscador_midias.strategy.measure import measure_items, section_of
from buscador_midias.strategy.scoring import ScoringPolicy, judge, score

# Teto por fonte: quanto maior, mais seções (temas) do site entram no mapa.
DEFAULT_MAX_PAGES = 50
# Menos que isto não é uma estratégia: pode ser só o destaque da home.
MIN_ARTICLES = 8

_HREF = re.compile(r"""<a\s[^>]*?href=["']([^"']+)["']""", re.IGNORECASE)
_ASSET = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|css|js|pdf|xml|json|zip|mp[34]|webm|woff2?)$", re.IGNORECASE
)
_SLUG_EXTENSION = re.compile(r"\.(html?|shtml|ghtml|php|aspx?)$", re.IGNORECASE)
_DATE_PATH = re.compile(r"/(?:19|20)\d{2}/\d{1,2}(?:/\d{1,2})?/")
_LONG_NUMBER = re.compile(r"\d{5,}")
_SECTION_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+){0,2}")
_MAX_SECTION_SEGMENTS = 3
_MAX_SECTION_WORD = 28
# Páginas de serviço, não de conteúdo: nunca são seção nem matéria.
_SERVICE_WORDS = frozenset(
    {
        "login",
        "logout",
        "signin",
        "signup",
        "register",
        "about",
        "about-us",
        "contact",
        "contact-us",
        "contato",
        "sobre",
        "privacy",
        "privacy-policy",
        "privacidade",
        "terms",
        "terms-and-conditions",
        "termos",
        "cookies",
        "faq",
        "help",
        "ajuda",
        "subscribe",
        "assine",
        "newsletter",
        "newsletters",
        "rss",
        "feed",
        "search",
        "busca",
        "my-profile",
        "profile",
        "account",
        "advertise",
        "anuncie",
        "sitemap",
        "jobs",
        "careers",
    }
)


@dataclass(frozen=True, slots=True)
class ListingFailure:
    """Por que a home não pôde ser varrida."""

    verdict: Verdict
    detail: str


@dataclass(frozen=True, slots=True)
class ListingScan:
    """O que a varredura achou.

    Attributes:
        articles: URLs com cara de matéria, na ordem em que apareceram.
        sections: URLs de seção vistas (visitadas ou não).
        pages: Páginas lidas com sucesso.
        requests: Páginas pedidas (inclui as que falharam).
        size_bytes: Bytes baixados.
        failure: Preenchido só quando a home não pôde ser lida.
    """

    articles: tuple[str, ...]
    sections: tuple[str, ...]
    pages: int
    requests: int
    size_bytes: int = 0
    failure: ListingFailure | None = None


def extract_links(html: bytes, base_url: str, site: Site) -> list[str]:
    """Os links da página que são do site: resolvidos, sem âncora, ativos e sem repetição."""
    text = html.decode("utf-8", errors="replace")
    seen: dict[str, None] = {}
    for raw in _HREF.findall(text):
        href = raw.strip()
        if href.lower().startswith(("mailto:", "javascript:", "tel:", "data:")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or _ASSET.search(parts.path):
            continue
        if parts.path not in ("", "/"):
            url = url.rstrip("/")
        if site.owns(url):
            seen.setdefault(url, None)
    return list(seen)


def is_article_url(url: str) -> bool:
    """Se a URL tem cara de matéria: *slug* longo, data no caminho ou número de identificação."""
    if "?" in url or l0_filter(url) is not None:
        return False
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if not segments or any(s.lower() in _SERVICE_WORDS for s in segments):
        return False
    last = _SLUG_EXTENSION.sub("", segments[-1])
    if last.count("-") >= 3 and len(last) >= 20:
        return True
    if _DATE_PATH.search(urlsplit(url).path + "/") and len(last) >= 8:
        return True
    return any(_LONG_NUMBER.search(segment) for segment in segments)


def is_section_url(url: str) -> bool:
    """Se a URL tem cara de página de seção: um caminho curto de palavras, sem números."""
    if "?" in url:
        return False
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if not segments or len(segments) > _MAX_SECTION_SEGMENTS:
        return False
    return all(
        _SECTION_WORD.fullmatch(s) is not None
        and len(s) <= _MAX_SECTION_WORD
        and s.lower() not in _SERVICE_WORDS
        for s in segments
    )


def scan_listing(
    site: Site,
    fetcher: Fetcher,
    *,
    robots: RobotsPolicy,
    user_agent: str,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> ListingScan:
    """Varre a home e as páginas de seção, até ``max_pages`` pedidos.

    As seções da home vêm primeiro; as achadas nas páginas de seção entram no fim da fila, e só são
    visitadas se sobrar teto. Uma página de seção que falha ou que o ``robots.txt`` proíbe não
    derruba a varredura; a home que não abre, sim.
    """
    queue: deque[str] = deque([site.url])
    queued = {site.url}
    articles: dict[str, None] = {}
    sections: dict[str, None] = {}
    pages = requests = size = 0
    while queue and requests < max_pages:
        url = queue.popleft()
        first = requests == 0
        if not robots.allows(url, user_agent):
            if first:
                return ListingScan(
                    (), (), 0, 0, 0, ListingFailure(Verdict.DISALLOWED, "robots.txt")
                )
            continue
        requests += 1
        result = fetcher.get(url)
        if not result.ok:
            if first:
                verdict = (
                    Verdict.BLOCKED if result.status in BLOCKING_STATUSES else Verdict.UNAVAILABLE
                )
                detail = f"HTTP {result.status}" if result.status else (result.error or "falha")
                return ListingScan((), (), 0, requests, 0, ListingFailure(verdict, detail))
            continue
        pages += 1
        size += len(result.content)
        for link in extract_links(result.content, result.final_url or url, site):
            if is_article_url(link):
                articles.setdefault(link, None)
            elif is_section_url(link) and link != site.url.rstrip("/"):
                sections.setdefault(link, None)
                if link not in queued:
                    queued.add(link)
                    queue.append(link)
    return ListingScan(tuple(articles), tuple(sections), pages, requests, size)


def evaluate_listing(
    site: Site,
    fetcher: Fetcher,
    *,
    robots: RobotsPolicy,
    user_agent: str,
    now: datetime,
    scoring: ScoringPolicy | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> CandidateReport:
    """A varredura como candidata: sem datas (``UNDATED``) e com a nota mais baixa de todas.

    Menos de ``MIN_ARTICLES`` links de matéria não é estratégia (``EMPTY``).
    """
    scoring = scoring or ScoringPolicy()
    scan = scan_listing(site, fetcher, robots=robots, user_agent=user_agent, max_pages=max_pages)
    url, origin = site.url, CandidateOrigin.HOME_LINK
    if scan.failure is not None:
        return CandidateReport(
            url,
            origin,
            scan.failure.verdict,
            None,
            None,
            0.0,
            scan.failure.detail,
            requests=scan.requests,
        )
    items = [Item(u, None, None, section_of(u, site.path_prefix)) for u in scan.articles]
    measure = measure_items(StrategyKind.LISTING, items, now=now, size_bytes=scan.size_bytes)
    detail = (
        f"varredura: {scan.pages} páginas, {len(scan.articles)} links de matéria, "
        f"{len(scan.sections)} seções vistas"
    )
    if len(items) < MIN_ARTICLES:
        return CandidateReport(
            url,
            origin,
            Verdict.EMPTY,
            StrategyKind.LISTING,
            measure,
            0.0,
            detail,
            requests=scan.requests,
        )
    verdict = judge(measure, scoring, now=now)
    return CandidateReport(
        url,
        origin,
        verdict,
        StrategyKind.LISTING,
        measure,
        score(measure, verdict, scoring),
        detail,
        requests=scan.requests,
    )
