"""Fontes por seção: de cada seção do site, o feed ou sitemap que a serve.

Não há um jeito único de achá-las, então há três mecanismos, do mais certo ao mais palpite:

1. o que a própria página aponta: feeds declarados em ``<link rel="alternate">``, links diretos para
   feeds e as **páginas-índice de feeds** (a Folha tem uma lista de feeds feita à mão);
2. a generalização por **padrão de URL**: se ``/rss/g1/economia/`` serve e ``economia`` é uma seção
   do site, ``/rss/g1/{section}/`` é um modelo a testar nas outras seções;
3. a correção manual (``data/hints_manuais.csv``), onde nada disso basta.

Aqui fica a parte pura (sem rede): reconhecer links e derivar modelos. Quem baixa e mede é a
sondagem.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from buscador_midias.discovery.feeds import find_feed_links
from buscador_midias.domain.site import Site

_HREF = re.compile(r"""<a\s[^>]*?href=["']([^"']+)["']""", re.IGNORECASE)
# "feed", "feeds", "rss" ou "atom" como palavra do caminho: "feedback" não conta.
_FEED_WORD = re.compile(r"(?:^|[/_.\-])(?:feeds?|rss|atom)(?:$|[/_.\-])", re.IGNORECASE)
_FEED_EXTENSION = re.compile(r"\.(?:xml|rss|atom)$", re.IGNORECASE)
_HTML_EXTENSION = re.compile(r"\.(?:html?|shtml|ghtml|php|aspx?)$", re.IGNORECASE)
MODEL_MARK = "{section}"


@dataclass(frozen=True, slots=True)
class PageSources:
    """O que uma página aponta como fonte de matérias.

    Attributes:
        declared: Feeds declarados em ``<link rel="alternate">`` (os mais confiáveis).
        direct: Links que parecem feeds ou sitemaps (``rss.xml``, ``/feeds/politica/``); quem os
            mede é a sondagem, e um que for HTML é descartado.
        indexes: Páginas HTML que parecem listar feeds (``lista-de-feeds.shtml``): vale ler uma
            e colher os links dela.
    """

    declared: tuple[str, ...]
    direct: tuple[str, ...]
    indexes: tuple[str, ...]


def find_feed_sources(html: bytes, base_url: str, site: Site) -> PageSources:
    """Separa o que a página aponta: feeds declarados, links diretos e páginas-índice de feeds.

    Só vale o que é do próprio site (host ou subdomínio); feed de comentários fica de fora.
    """
    declared = tuple(u for u in find_feed_links(html, base_url) if site.owns_host(u))
    direct: dict[str, None] = {}
    indexes: dict[str, None] = {}
    text = html.decode("utf-8", errors="replace")
    for raw in _HREF.findall(text):
        url = urldefrag(urljoin(base_url, raw.strip()))[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not site.owns_host(url):
            continue
        if "comment" in url.lower() or url in declared:
            continue
        if not (_FEED_EXTENSION.search(parts.path) or _FEED_WORD.search(parts.path)):
            continue
        if _HTML_EXTENSION.search(parts.path):
            indexes.setdefault(url, None)
        else:
            direct.setdefault(url, None)
    return PageSources(declared, tuple(direct), tuple(indexes))


def derive_templates(known: Iterable[str], sections: Sequence[str]) -> tuple[str, ...]:
    """Modelos de URL a partir de fontes conhecidas que trazem o caminho de uma seção.

    ``/rss/g1/economia/`` com ``economia`` entre as seções vira ``/rss/g1/{section}/``. A seção
    tem de aparecer inteira, em segmentos (``economia-global`` não é ``economia``); com mais de
    uma seção na URL vale a mais longa (``ac/acre`` antes de ``ac``).
    """
    section_paths = sorted(
        {tuple(s for s in urlsplit(url).path.split("/") if s) for url in sections},
        key=len,
        reverse=True,
    )
    templates: dict[str, None] = {}
    for url in known:
        parts = urlsplit(url)
        segments = [s for s in parts.path.split("/") if s]
        for path in section_paths:
            if not path:
                continue
            at = _find(segments, path)
            if at is None:
                continue
            new_path = "/".join([*segments[:at], MODEL_MARK, *segments[at + len(path) :]])
            trailing = "/" if parts.path.endswith("/") else ""
            templates.setdefault(
                urlunsplit((parts.scheme, parts.netloc, f"/{new_path}{trailing}", parts.query, "")),
                None,
            )
            break
    return tuple(templates)


def apply_templates(
    templates: Sequence[str],
    sections: Sequence[str],
    known: Collection[str],
    *,
    limit: int,
) -> list[str]:
    """As URLs que os modelos dão para cada seção, sem repetir o que já se conhece."""
    out: dict[str, None] = {}
    for template in templates:
        for section in sections:
            path = "/".join(s for s in urlsplit(section).path.split("/") if s)
            url = template.replace(MODEL_MARK, path)
            if url not in known and url not in out:
                out[url] = None
            if len(out) >= limit:
                return list(out)
    return list(out)


def _find(segments: list[str], path: tuple[str, ...]) -> int | None:
    """Onde a sequência ``path`` aparece, inteira e sem diferença de caixa, em ``segments``."""
    size = len(path)
    wanted = [p.lower() for p in path]
    for start in range(len(segments) - size + 1):
        if [s.lower() for s in segments[start : start + size]] == wanted:
            return start
    return None
