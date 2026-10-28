"""Mapeamento retomável da estrutura do site, com checkpoint por página."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from heapq import heappop, heappush
from itertools import count
from typing import Protocol
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from lxml import etree
from lxml import html as lxml_html

from buscador_midias.discovery.explore import BLOCKING_STATUSES, Exploration, read_robots
from buscador_midias.domain.mapping import MapPage, MapSource, MapSummary, PageStatus
from buscador_midias.domain.site import Site
from buscador_midias.extraction.html import decode_html
from buscador_midias.extraction.reliability import (
    looks_like_challenge_page,
    looks_like_error_page,
)
from buscador_midias.net.fetcher import Fetcher, FetchResult
from buscador_midias.strategy.listing import is_article_url, is_section_url
from buscador_midias.strategy.mapping_net import (
    ORIGIN_FAILURE_LIMIT,
    MappingFetcher,
    is_transport_failure,
)
from buscador_midias.strategy.page_classification import classify_page, classify_url
from buscador_midias.strategy.section_sources import find_feed_sources

_TAXONOMIES = frozenset(
    {
        "tag",
        "tags",
        "author",
        "authors",
        "autor",
        "autores",
        "keyword",
        "topic",
        "topics",
        "topics-list",
        "themen",
        "tagg",
        "amnen",
        "persons",
        "mot-cle",
        "mots-cles",
        "tema",
        "topico",
    }
)
_REPEATED_SEGMENT = re.compile(r"(?:^|/)([^/]+)/\1/\1(?:/|$)", re.IGNORECASE)
_LOOP_DETAIL = "segmento repetido três vezes; revisar possível ciclo de navegação"
_FAILURES = frozenset(
    {PageStatus.BLOCKED, PageStatus.ERROR, PageStatus.DISALLOWED, PageStatus.INVALID}
)
_MENU_MARKERS = frozenset(
    {
        "menu",
        "main-menu",
        "primary-menu",
        "site-menu",
        "navbar",
        "navigation",
        "main-nav",
        "primary-nav",
        "site-nav",
        "header-nav",
        "nav-menu",
        "menu-item",
    }
)


def _navigation_element(element: etree._Element) -> bool:
    """Reconhece contêineres de menu declarados por semântica ou identificador CSS."""
    if element.tag == "nav" or element.get("role") == "navigation":
        return True
    if element.tag not in ("ul", "ol", "li", "div", "header"):
        return False
    markers = (element.get("class", "") + " " + element.get("id", "")).casefold().split()
    return any(marker in _MENU_MARKERS for marker in markers)


class MappingStore(Protocol):
    """Porta do inventário; cada lote deve ser gravado atomicamente."""

    def mapping_pages(self, site_key: str) -> list[MapPage]:
        """Todas as páginas da fonte, em ordem de descoberta."""
        ...

    def save_mapping_pages(self, site_key: str, pages: Sequence[MapPage]) -> None:
        """Grava a página processada e as novas pendências na mesma transação."""
        ...


def normalize_page(url: str) -> str:
    """Remove fragmento e barra final, mantendo caminhos, esquema e query."""
    parts = urlsplit(urldefrag(url)[0])
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def is_taxonomy_url(url: str) -> bool:
    """Diretórios de tags, autores e palavras-chave reconhecidos no inventário."""
    return any(s.casefold() in _TAXONOMIES for s in urlsplit(url).path.split("/"))


def _is_section_candidate(url: str) -> bool:
    """Aplica a heurística de palavras a cada segmento, sem cortar subseções profundas."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    return (
        bool(segments)
        and not parts.query
        and all(
            is_section_url(urlunsplit((parts.scheme, parts.netloc, "/" + s, "", "")))
            for s in segments
        )
    )


def summarize_map(site_key: str, pages: Sequence[MapPage], detail: str = "") -> MapSummary:
    """Resume a fila sem converter sucesso de HTTP em cobertura validada."""
    return MapSummary(
        site_key=site_key,
        sections=sum(p.kind == "section" and p.is_section_candidate for p in pages),
        visited=sum(p.status is PageStatus.OK and p.is_section_candidate for p in pages),
        pending=sum(p.status is PageStatus.PENDING for p in pages),
        issues=sum(p.status in _FAILURES or (p.kind == "root" and bool(p.detail)) for p in pages),
        excluded=sum(p.status is PageStatus.EXCLUDED for p in pages),
        detail=detail,
    )


def map_site(
    site: Site,
    fetcher: Fetcher,
    store: MappingStore,
    *,
    user_agent: str,
    now: datetime,
    max_pages: int = 50,
    retry_failed: bool = False,
    refresh_pages: bool = False,
    suspended_origins: dict[str, str] | None = None,
) -> MapSummary:
    """Explora um lote e persiste a fila, sem baixar os links de matéria nem feeds.

    A próxima chamada continua pelas páginas pendentes. Falhas só voltam à fila com
    ``retry_failed``. A política robots é relida em cada lote para hosts não suspensos.
    O limite conta páginas processadas (inclusive proibidas), além das leituras de robots.
    """
    if max_pages < 1:
        raise ValueError("max_pages precisa ser positivo")
    pages = {p.url: p for p in store.mapping_pages(site.key)}
    # Classificações novas também se aplicam às filas preservadas de execuções anteriores.
    reopened = [
        replace(
            p,
            page_type="unknown",
            type_basis="",
            status=PageStatus.PENDING
            if (
                _is_section_candidate(p.url)
                or (p.discovery_basis == "navigation" and _navigation_section(p.url))
            )
            else PageStatus.EXCLUDED,
            detail="regra de serviço revisada; reavaliar pela heurística de seção",
        )
        for p in pages.values()
        if p.kind != "root"
        and p.page_type == "service"
        and p.type_basis == "observed_service_route"
        and classify_url(p.url).page_type == "unknown"
    ]
    if reopened:
        store.save_mapping_pages(site.key, reopened)
        pages.update((p.url, p) for p in reopened)
    taxonomies = [
        replace(
            p,
            kind="taxonomy",
            status=PageStatus.EXCLUDED,
            page_type="taxonomy",
            type_basis="taxonomy_path",
            detail="taxonomia; revisar se necessário",
        )
        for p in pages.values()
        if p.kind != "root" and p.page_type != "taxonomy" and is_taxonomy_url(p.url)
    ]
    if taxonomies:
        store.save_mapping_pages(site.key, taxonomies)
        pages.update((p.url, p) for p in taxonomies)
    loops = [
        replace(p, status=PageStatus.EXCLUDED, detail=_LOOP_DETAIL)
        for p in pages.values()
        if p.kind != "root"
        and p.status is not PageStatus.EXCLUDED
        and _REPEATED_SEGMENT.search(urlsplit(p.url).path)
    ]
    if loops:
        store.save_mapping_pages(site.key, loops)
        pages.update((p.url, p) for p in loops)
    services = [
        replace(
            p,
            status=PageStatus.EXCLUDED,
            page_type="service",
            type_basis=classify_url(p.url).basis,
            detail="rota de serviço",
        )
        for p in pages.values()
        if p.kind != "root"
        and p.page_type != "service"
        and classify_url(p.url).page_type == "service"
    ]
    if services:
        store.save_mapping_pages(site.key, services)
        pages.update((p.url, p) for p in services)
    root = normalize_page(site.url)
    if root not in pages:
        pages[root] = MapPage(url=root, found_on="", kind="root", discovery_basis="entrypoint")
        store.save_mapping_pages(site.key, [pages[root]])
    queue: list[tuple[int, int, int, str]] = []
    sequence = count()
    processed: set[str] = set()

    def eligible(p: MapPage) -> bool:
        return (p.kind == "root" or p.is_section_candidate) and (
            p.status is PageStatus.PENDING
            or (retry_failed and p.status in _FAILURES)
            or (refresh_pages and p.status is PageStatus.OK)
        )

    def enqueue(p: MapPage) -> None:
        rank = 0 if p.kind == "root" else 1 if p.discovery_basis == "navigation" else 2
        path = urlsplit(p.url).path.removeprefix(site.path_prefix)
        depth = len([part for part in path.split("/") if part])
        heappush(queue, (rank, depth, next(sequence), p.url))

    for p in pages.values():
        if eligible(p):
            enqueue(p)
    if not queue:
        return summarize_map(site.key, list(pages.values()))
    suspended = suspended_origins if suspended_origins is not None else {}
    root_origin = urlsplit(site.url).netloc
    robots = (
        None
        if root_origin in suspended
        else read_robots(site, fetcher, user_agent, require_root=False)
    )
    if isinstance(robots, Exploration):
        suspended[root_origin] = (
            f"{robots.detail or robots.status.value}; origem {root_origin}; fila preservada"
        )
        robots = None
    if not suspended and pages[root].detail and pages[root].status is PageStatus.OK:
        pages[root] = replace(pages[root], detail="")
        store.save_mapping_pages(site.key, [pages[root]])

    def save_suspensions() -> None:
        if suspended:
            detail = "; ".join(suspended.values())
            if pages[root].detail != detail:
                pages[root] = replace(pages[root], detail=detail)
                store.save_mapping_pages(site.key, [pages[root]])

    save_suspensions()
    guarded = MappingFetcher(fetcher, site, user_agent, max_pages * 2 + 1)
    if robots is not None:
        guarded.seed_policy(site.url, robots)
    failures: dict[str, int] = {}
    for _ in range(max_pages):
        while queue and (queue[0][3] in processed or urlsplit(queue[0][3]).netloc in suspended):
            heappop(queue)
        if not queue:
            break
        url = heappop(queue)[3]
        processed.add(url)
        previous = pages[url]
        by_url = classify_url(url)
        if by_url.page_type == "service":
            current = replace(
                previous,
                page_type="service",
                type_basis=by_url.basis,
                status=PageStatus.INVALID if previous.kind == "root" else PageStatus.EXCLUDED,
                detail="rota de serviço",
                checked_at=now,
                article_urls=(),
                feeds=(),
                feed_indexes=(),
            )
            children: list[MapPage] = []
        elif (
            robots is not None
            and urlsplit(url).netloc == root_origin
            and not robots.allows(url, user_agent)
        ):
            current = replace(
                previous, status=PageStatus.DISALLOWED, detail="robots.txt", checked_at=now
            )
            children = []
        else:
            result = guarded.get(url)
            current, children = _inspect(site, previous, result, now)
            origin = urlsplit(url).netloc
            failures[origin] = failures.get(origin, 0) + 1 if is_transport_failure(result) else 0
            if failures[origin] >= ORIGIN_FAILURE_LIMIT:
                suspended[origin] = (
                    f"exploração suspensa após {ORIGIN_FAILURE_LIMIT} falhas consecutivas "
                    f"de conexão em {origin}: {current.detail}; fila preservada"
                )
        updates = [current]
        for child in children:
            if child.url not in pages:
                pages[child.url] = child
                updates.append(child)
                if child.status is PageStatus.PENDING:
                    enqueue(child)
            elif child.discovery_basis == "navigation" and pages[child.url].discovery_basis in (
                "url_shape",
                "mechanism_path",
            ):
                pages[child.url] = replace(pages[child.url], discovery_basis="navigation")
                if pages[child.url].status is PageStatus.EXCLUDED and pages[
                    child.url
                ].detail.startswith("candidata refutada:"):
                    pages[child.url] = replace(
                        pages[child.url],
                        status=PageStatus.PENDING,
                        detail="navegação atual voltou a apontar candidata antes refutada",
                    )
                updates.append(pages[child.url])
                if child.url not in processed and eligible(pages[child.url]):
                    enqueue(pages[child.url])
        store.save_mapping_pages(site.key, updates)
        pages[url] = current
        save_suspensions()
    active = any(
        p.status is PageStatus.PENDING
        and (p.kind == "root" or p.is_section_candidate)
        and urlsplit(p.url).netloc not in suspended
        for p in pages.values()
    )
    detail = "; ".join(suspended.values()) if suspended and not active else ""
    return summarize_map(site.key, list(pages.values()), detail)


def drain_site(
    site: Site,
    fetcher: Fetcher,
    store: MappingStore,
    *,
    user_agent: str,
    clock: Callable[[], datetime],
    batch_pages: int = 100,
    retry_failed: bool = False,
    refresh_pages: bool = False,
    on_batch: Callable[[MapSummary], None] | None = None,
    suspended_origins: dict[str, str] | None = None,
) -> MapSummary:
    """Continua até esgotar as pendências reconhecidas ou não conseguir avançar.

    Falhas e releituras entram apenas no primeiro lote. Não transforma pendências
    bloqueadas em páginas concluídas; o motivo e os checkpoints continuam no inventário.
    """
    first = True
    suspended = suspended_origins if suspended_origins is not None else {}
    while True:
        before = {p.url for p in store.mapping_pages(site.key) if p.status is PageStatus.PENDING}
        result = map_site(
            site,
            fetcher,
            store,
            user_agent=user_agent,
            now=clock(),
            max_pages=batch_pages,
            retry_failed=retry_failed and first,
            refresh_pages=refresh_pages and first,
            suspended_origins=suspended,
        )
        first = False
        if on_batch is not None:
            on_batch(result)
        after = {p.url for p in store.mapping_pages(site.key) if p.status is PageStatus.PENDING}
        if result.detail or not after or after == before:
            return result


def sections_from_sources(
    site: Site, pages: Sequence[MapPage], checks: Sequence[MapSource]
) -> list[MapPage]:
    """Deriva candidatas dos caminhos de URLs em feeds/sitemaps já lidos.

    Um prefixo de URL é apenas pista estrutural. Mantém escopo, taxonomias e serviços,
    não reabre páginas conhecidas e guarda o mecanismo que revelou cada caminho.
    """
    known = {page.url for page in pages}
    found: list[MapPage] = []
    for source in checks:
        if source.kind not in ("feed", "sitemap", "news_sitemap") or source.verdict not in (
            "fresh",
            "undated",
            "slow",
            "stale",
            "archive",
            "empty",
        ):
            continue
        for article in source.listed_urls:
            if not site.owns(article):
                continue
            parts = urlsplit(article)
            segments = [part for part in parts.path.split("/") if part]
            for depth in range(1, len(segments)):
                prefix = segments[:depth]
                url = normalize_page(
                    urlunsplit((parts.scheme, parts.netloc, "/" + "/".join(prefix), "", ""))
                )
                if (
                    url in known
                    or not site.owns(url)
                    or not _is_section_candidate(url)
                    or any(part.lower() in _TAXONOMIES for part in prefix)
                    or classify_url(url).page_type == "service"
                    or _REPEATED_SEGMENT.search(urlsplit(url).path)
                ):
                    continue
                known.add(url)
                found.append(
                    MapPage(
                        url,
                        source.url,
                        "section",
                        discovery_basis="mechanism_path",
                        detail="prefixo de URL em mecanismo lido; seção ainda não confirmada",
                    )
                )
    return found


def _inspect(
    site: Site,
    page: MapPage,
    result: FetchResult,
    now: datetime,
) -> tuple[MapPage, list[MapPage]]:
    """Interpreta uma resposta de listagem sem aceitar bloqueios como seções vazias."""
    current = replace(page, attempts=page.attempts + 1, checked_at=now)

    def failure(status: PageStatus, detail: str) -> tuple[MapPage, list[MapPage]]:
        return replace(current, status=status, detail=detail), []

    if result.error and "robots.txt proíbe" in result.error:
        return failure(PageStatus.DISALLOWED, result.error)
    if not result.ok:
        return failure(
            PageStatus.BLOCKED if result.status in BLOCKING_STATUSES else PageStatus.ERROR,
            f"HTTP {result.status}" if result.status else (result.error or "falha de rede"),
        )
    base = result.final_url or page.url
    if not site.owns(base) or (
        page.kind != "root" and normalize_page(base) == normalize_page(site.url)
    ):
        target = urlsplit(base)
        destination = urlunsplit((target.scheme, target.netloc, target.path, "", ""))
        return failure(
            PageStatus.INVALID, f"redirecionamento para fora da seção/fonte: {destination}"
        )
    if result.content_type and not any(
        t in result.content_type.lower() for t in ("text/html", "application/xhtml+xml")
    ):
        return failure(PageStatus.INVALID, "resposta não é HTML")
    try:
        tree = lxml_html.fromstring(
            decode_html(result.content, result.content_type).encode("utf-8"),
            parser=lxml_html.HTMLParser(encoding="utf-8"),
        )
    except (etree.ParserError, ValueError):
        return failure(PageStatus.INVALID, "HTML vazio ou ilegível")
    title_node = tree.find(".//title")
    title = " ".join(str(t) for t in title_node.itertext()) if title_node is not None else ""
    text = " ".join(str(t) for t in tree.itertext())
    if looks_like_challenge_page(title + " " + text, text_chars=len(text)):
        return failure(PageStatus.BLOCKED, "desafio de bot")
    if looks_like_error_page(result.content):
        return failure(PageStatus.INVALID, "página de erro com HTTP 200")
    classification = classify_page(result.content, base, parsed=tree)
    current = replace(current, page_type=classification.page_type, type_basis=classification.basis)
    if classification.page_type in ("article", "service"):
        return replace(
            current,
            status=PageStatus.INVALID if page.kind == "root" else PageStatus.EXCLUDED,
            title=title,
            article_urls=(),
            feeds=(),
            feed_indexes=(),
            detail=f"{classification.page_type}: {classification.basis}",
        ), []
    article_metadata = page.kind != "root" and any(
        m.get("property") == "og:type" and m.get("content") == "article" for m in tree.iter("meta")
    )
    children: dict[str, MapPage] = {}
    articles: dict[str, None] = {}
    has_main = any(True for _ in tree.iter("main"))
    for anchor in tree.iter("a"):
        href = anchor.get("href")
        if not href:
            continue
        try:
            url = normalize_page(urljoin(base, href.strip()))
        except ValueError:
            continue  # Um href inválido não descarta a seção nem seus outros links.
        if not site.owns(url) or url in (normalize_page(site.url), page.url, normalize_page(base)):
            continue
        ancestors = list(anchor.iterancestors())
        navigation = any(_navigation_element(a) for a in ancestors)
        discovery = "navigation" if navigation else "url_shape"
        segments = urlsplit(url).path.lower().split("/")
        if _REPEATED_SEGMENT.search(urlsplit(url).path):
            children.setdefault(
                url,
                MapPage(
                    url,
                    page.url,
                    "section",
                    PageStatus.EXCLUDED,
                    detail=_LOOP_DETAIL,
                    discovery_basis=discovery,
                ),
            )
        elif any(s in _TAXONOMIES for s in segments):
            children.setdefault(
                url,
                MapPage(
                    url,
                    page.url,
                    "taxonomy",
                    PageStatus.EXCLUDED,
                    detail="taxonomia; revisar se necessário",
                    page_type="taxonomy",
                    type_basis="taxonomy_path",
                    discovery_basis=discovery,
                ),
            )
        elif classify_url(url).page_type == "service":
            children.setdefault(
                url,
                MapPage(
                    url,
                    page.url,
                    "section",
                    PageStatus.EXCLUDED,
                    page_type="service",
                    type_basis=classify_url(url).basis,
                    discovery_basis=discovery,
                    detail="rota de serviço",
                ),
            )
        elif is_article_url(url):
            if not any(
                a.tag in ("footer", "aside") or _navigation_element(a) for a in ancestors
            ) and (not has_main or any(a.tag == "main" for a in ancestors)):
                articles[url] = None
        elif _is_section_candidate(url) or (navigation and _navigation_section(url)):
            candidate = MapPage(url, page.url, "section", discovery_basis=discovery)
            if url not in children or discovery == "navigation":
                children[url] = candidate
    sources = find_feed_sources(result.content, base, site)
    current = replace(
        current,
        status=PageStatus.OK,
        title=title,
        detail="metadado article; confirmar se é seção" if article_metadata else "",
        article_urls=tuple(articles),
        feeds=tuple(dict.fromkeys(sources.declared + sources.direct)),
        feed_indexes=sources.indexes,
    )
    return current, list(children.values())


def _navigation_section(url: str) -> bool:
    """Reconhece rotas curtas com extensão somente quando a navegação as aponta."""
    parts = urlsplit(url)
    path = re.sub(r"\.(?:html?|shtml|php|aspx?)$", "", parts.path, flags=re.IGNORECASE)
    return path != parts.path and _is_section_candidate(
        urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
    )
