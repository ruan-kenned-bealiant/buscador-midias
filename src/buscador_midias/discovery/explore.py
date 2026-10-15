"""Caminhamento por robots.txt e sitemaps de um site, compartilhado por sondagem e descoberta."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.discovery.sitemap_parser import SitemapParseError, parse_sitemap
from buscador_midias.domain.site import Site
from buscador_midias.domain.sitemap import ParsedSitemap, SitemapKind, SitemapRef
from buscador_midias.net.fetcher import Fetcher, FetchResult

DEFAULT_MAX_SITEMAP_FETCHES = 6
_FALLBACK_PATHS = ("/sitemap.xml", "/sitemap_index.xml")
_BLOCKING_STATUSES = frozenset({401, 403, 429})
_NO_ROBOTS_STATUSES = frozenset({404, 410})
# Palavras do nome do arquivo que indicam sitemap de listagens (tags, autores...), não de matérias.
_TAXONOMY_WORDS = frozenset(
    {
        "tag",
        "tags",
        "category",
        "categories",
        "author",
        "authors",
        "user",
        "users",
        "person",
        "people",
        "keyword",
        "keywords",
        "taxonomy",
        "taxonomies",
        "topic",
        "topics",
        "page",
        "pages",
        "static",
        "channel",
        "image",
        "images",
    }
)


class ProbeStatus(StrEnum):
    """Desfecho da exploração de um site."""

    OK = "ok"
    NO_SITEMAP = "no_sitemap"
    BLOCKED = "blocked"
    ROBOTS_DISALLOWED = "robots_disallowed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ExploredSitemap:
    """Um sitemap lido e interpretado com sucesso.

    Attributes:
        url: URL do sitemap.
        sitemap: Conteúdo interpretado.
        declared_lastmod: ``lastmod`` que o índice-pai declarou para este sitemap (``None`` para
            sitemaps de entrada ou quando o índice não informa).
        is_entry: Se é um sitemap de entrada (declarado no robots ou caminho padrão).
    """

    url: str
    sitemap: ParsedSitemap
    declared_lastmod: datetime | None
    is_entry: bool


@dataclass(frozen=True, slots=True)
class Exploration:
    """Resultado do caminhamento pelos sitemaps de um site.

    Attributes:
        status: ``OK`` se houve sitemap lido e utilizável; senão o motivo da falha.
        detail: Explicação curta da falha (vazio quando ``OK``).
        sitemaps: Sitemaps lidos com sucesso, na ordem de leitura.
        fetched: Quantos sitemaps foram requisitados (conta no orçamento).
        skipped: Quantos sitemaps filhos foram dispensados por ``skip_child`` (sem requisição).
        failed: Sitemaps que não puderam ser lidos (rede, bloqueio, XML inválido, robots) em uma
            exploração que, mesmo assim, foi ``OK``: sinal de cobertura parcial.
        failure_detail: A falha mais informativa dentre as de ``failed`` (vazio sem falhas).
    """

    status: ProbeStatus
    detail: str
    sitemaps: tuple[ExploredSitemap, ...]
    fetched: int
    skipped: int
    failed: int = 0
    failure_detail: str = ""


def explore_site(
    site: Site,
    fetcher: Fetcher,
    *,
    user_agent: str,
    max_sitemap_fetches: int = DEFAULT_MAX_SITEMAP_FETCHES,
    skip_child: Callable[[SitemapRef], bool] | None = None,
) -> Exploration:
    """Lê o robots.txt e percorre os sitemaps do site dentro de um orçamento de leituras.

    Respeita o robots.txt. Em um índice, prioriza filhos que citam a seção da fonte, depois os com
    "news" no nome, depois o ``lastmod`` mais recente (sem ele, os listados ao fim do índice).
    Filhos que são só listagens (tags, autores, categorias, páginas estáticas) não são lidos
    enquanto houver algum sitemap de matérias: eles gastariam o orçamento sem trazer notícias.
    Todos os sitemaps declarados no robots.txt são lidos antes dos filhos de um índice; os filhos
    ocupam só o orçamento que sobra.

    Args:
        site: Fonte a explorar.
        fetcher: Porta HTTP (use a versão com intervalo por host em produção).
        user_agent: Nome do nosso agente, usado nas regras do robots.txt.
        max_sitemap_fetches: Orçamento de sitemaps requisitados.
        skip_child: Se informado, é consultado para cada filho da janela do orçamento de um índice
            de entrada; ``True`` dispensa o filho sem requisição (ex.: ``lastmod`` inalterado),
            mas ele continua ocupando um lugar na janela, então nunca se avança para filhos
            mais antigos.

    Returns:
        A exploração; nunca levanta por causa do conteúdo ou da rede do site.
    """
    origin = _origin(site.url)
    robots_result = fetcher.get(f"{origin}/robots.txt")
    policy = _read_robots(site, robots_result, user_agent)
    if isinstance(policy, Exploration):
        return policy

    terms = _section_terms(site)
    declared = policy.sitemaps or tuple(f"{origin}{path}" for path in _FALLBACK_PATHS)
    entry_points = _rank_urls(declared, terms)
    return _walk(
        site, fetcher, policy, entry_points, terms, user_agent, max_sitemap_fetches, skip_child
    )


def _failed(status: ProbeStatus, detail: str, *, fetched: int = 0, skipped: int = 0) -> Exploration:
    """Exploração sem sitemaps utilizáveis."""
    return Exploration(status=status, detail=detail, sitemaps=(), fetched=fetched, skipped=skipped)


def _read_robots(site: Site, result: FetchResult, user_agent: str) -> RobotsPolicy | Exploration:
    """Interpreta a resposta do robots.txt.

    Returns:
        A política a seguir, ou uma ``Exploration`` quando o desfecho já está decidido (bloqueio,
        erro ou acesso proibido pelo robots.txt).
    """
    if result.status in _BLOCKING_STATUSES:
        return _failed(ProbeStatus.BLOCKED, f"robots.txt: HTTP {result.status}")
    if result.status is None or result.error is not None:
        return _failed(ProbeStatus.ERROR, result.error or "falha de rede")
    if result.ok:
        policy = RobotsPolicy.parse(
            result.content.decode("utf-8", errors="replace"), base_url=_origin(site.url)
        )
    elif result.status in _NO_ROBOTS_STATUSES:
        policy = RobotsPolicy.allow_all()
    else:
        return _failed(ProbeStatus.ERROR, f"robots.txt: HTTP {result.status}")

    if not policy.allows(site.url, user_agent):
        return _failed(ProbeStatus.ROBOTS_DISALLOWED, "robots.txt proíbe o acesso à raiz")
    return policy


def _walk(
    site: Site,
    fetcher: Fetcher,
    policy: RobotsPolicy,
    entry_points: tuple[str, ...],
    terms: tuple[str, ...],
    user_agent: str,
    budget: int,
    skip_child: Callable[[SitemapRef], bool] | None,
) -> Exploration:
    """Percorre os sitemaps de entrada e filhos de índice dentro do orçamento de leituras."""
    queue: deque[str] = deque(entry_points)
    explored: list[ExploredSitemap] = []
    failures: list[tuple[ProbeStatus, str]] = []
    is_entry = set(entry_points)
    declared_lastmod: dict[str, datetime | None] = {}
    fetched = 0
    skipped = 0

    while queue and fetched < budget:
        url = queue.popleft()
        if not policy.allows(url, user_agent):
            failures.append((ProbeStatus.ROBOTS_DISALLOWED, f"{url}: proibido pelo robots.txt"))
            continue
        fetched += 1
        result = fetcher.get(url)
        failure = _classify_failure(url, result)
        if failure is not None:
            failures.append(failure)
            continue
        try:
            sitemap = parse_sitemap(result.content)
        except SitemapParseError as error:
            failures.append((ProbeStatus.ERROR, f"{url}: sitemap inválido: {error}"))
            continue
        explored.append(
            ExploredSitemap(
                url=url,
                sitemap=sitemap,
                declared_lastmod=declared_lastmod.get(url),
                is_entry=url in is_entry,
            )
        )
        if sitemap.kind is SitemapKind.INDEX and url in is_entry:
            kept: list[str] = []
            # O orçamento é uma janela dos filhos mais recentes: pular um filho inalterado
            # consome a janela em vez de avançar para arquivos mais antigos que nunca lemos.
            ranked = _prioritize(sitemap.children, terms)
            candidates = [child for child in ranked if not is_taxonomy_sitemap(child.loc)] or ranked
            # Os sitemaps declarados no robots.txt (ex.: o de notícias) têm vez antes dos filhos.
            reserved = sum(1 for u in queue if u in is_entry and policy.allows(u, user_agent))
            window = candidates[: max(budget - fetched - reserved, 0)]
            for child in window:
                if skip_child is not None and skip_child(child):
                    skipped += 1
                    continue
                declared_lastmod[child.loc] = child.lastmod
                kept.append(child.loc)
            queue.extend(kept)

    return _conclude(explored, failures, fetched, skipped)


def _conclude(
    explored: list[ExploredSitemap],
    failures: list[tuple[ProbeStatus, str]],
    fetched: int,
    skipped: int,
) -> Exploration:
    """Decide o desfecho: ``OK`` só se houve sitemap lido e, havendo falhas, ao menos uma URL."""
    if not explored:
        status, detail = _worst_failure(failures)
        if status is ProbeStatus.NO_SITEMAP:
            detail = "nenhum sitemap encontrado"
        return _failed(status, detail, fetched=fetched, skipped=skipped)

    has_urls = any(item.sitemap.entries for item in explored)
    if not has_urls and failures:
        status, detail = _worst_failure(failures)
        return _failed(
            status,
            f"sitemap lido, mas sem URLs utilizáveis: {detail}",
            fetched=fetched,
            skipped=skipped,
        )
    return Exploration(
        status=ProbeStatus.OK,
        detail="",
        sitemaps=tuple(explored),
        fetched=fetched,
        skipped=skipped,
        failed=len(failures),
        failure_detail=_worst_failure(failures)[1] if failures else "",
    )


def _classify_failure(url: str, result: FetchResult) -> tuple[ProbeStatus, str] | None:
    """Traduz uma resposta que não é utilizável em (status da exploração, detalhe)."""
    if result.ok:
        return None
    if result.status in _BLOCKING_STATUSES:
        return ProbeStatus.BLOCKED, f"{url}: HTTP {result.status}"
    if result.status in _NO_ROBOTS_STATUSES:
        return ProbeStatus.NO_SITEMAP, f"{url}: HTTP {result.status}"
    return ProbeStatus.ERROR, f"{url}: {result.error or f'HTTP {result.status}'}"


def is_taxonomy_sitemap(url: str) -> bool:
    """Indica se o sitemap lista tags, autores, categorias ou páginas estáticas, não matérias.

    Só o nome do arquivo conta (``post_tag-sitemap3.xml``, ``author-1.xml``): o host e as pastas
    não, para não confundir um site chamado "tags" com uma listagem.

    Args:
        url: URL do sitemap.

    Returns:
        ``True`` se alguma palavra do nome do arquivo é de listagem.
    """
    filename = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    return any(word in _TAXONOMY_WORDS for word in re.split(r"[^a-z]+", filename))


def _section_terms(site: Site) -> tuple[str, ...]:
    """Segmentos do prefixo de seção em minúsculas (``/pt-br`` vira ``("pt-br",)``)."""
    return tuple(part.lower() for part in site.path_prefix.split("/") if part)


def _mentions(url: str, terms: tuple[str, ...]) -> bool:
    """Indica se a URL cita algum termo da seção (sempre falso para domínio inteiro)."""
    lowered = url.lower()
    return any(term in lowered for term in terms)


def _rank_urls(urls: tuple[str, ...], terms: tuple[str, ...]) -> tuple[str, ...]:
    """Coloca antes as URLs que citam a seção da fonte, mantendo a ordem relativa."""
    return tuple(sorted(urls, key=lambda url: 0 if _mentions(url, terms) else 1))


def _prioritize(children: tuple[SitemapRef, ...], terms: tuple[str, ...]) -> list[SitemapRef]:
    """Ordena filhos de um índice.

    Prioridade: os que citam a seção da fonte, depois os com "news" no nome, depois o ``lastmod``
    mais recente e, na falta dele, os listados mais ao fim do índice.
    """

    def sort_key(item: tuple[int, SitemapRef]) -> tuple[int, int, float, int]:
        position, child = item
        not_section = 0 if _mentions(child.loc, terms) else 1
        not_news = 0 if "news" in child.loc.lower() else 1
        stamp = child.lastmod.timestamp() if child.lastmod else float("-inf")
        # Sem lastmod, índices costumam listar do mais antigo ao mais novo: ler do fim.
        return (not_section, not_news, -stamp, -position)

    return [child for _, child in sorted(enumerate(children), key=sort_key)]


def _worst_failure(failures: list[tuple[ProbeStatus, str]]) -> tuple[ProbeStatus, str]:
    """Escolhe a falha mais informativa: bloqueio > erro > robots > ausência de sitemap."""
    rank = {
        ProbeStatus.BLOCKED: 0,
        ProbeStatus.ERROR: 1,
        ProbeStatus.ROBOTS_DISALLOWED: 2,
        ProbeStatus.NO_SITEMAP: 3,
    }
    if not failures:
        return ProbeStatus.NO_SITEMAP, "nenhum sitemap encontrado"
    return min(failures, key=lambda f: rank[f[0]])


def _origin(url: str) -> str:
    """Esquema + host da URL (sem caminho), base de ``/robots.txt`` e dos caminhos padrão."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
