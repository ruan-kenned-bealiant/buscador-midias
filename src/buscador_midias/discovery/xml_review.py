"""Leitura de um único mecanismo para revisão finita, sem expansão ou filtro de data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lxml import etree

from buscador_midias.discovery.feeds import parse_feed, parse_feed_entries
from buscador_midias.discovery.sitemap_parser import parse_sitemap
from buscador_midias.net.fetcher import FetchResult


@dataclass(frozen=True)
class XmlReview:
    """Evidência de um documento; filhos são referências, não tarefas."""

    kind: str
    entries: tuple[str, ...]
    children: tuple[str, ...]
    podcast: bool = False


def review_xml(body: bytes, url: str) -> XmlReview:
    """Interpreta RSS/Atom ou sitemap, preservando artigos antigos como amostras elegíveis."""
    # Sitemaps comprimidos devem ir ao parser que limita a descompressão.
    feed = None if body.startswith((b"\x1f\x8b", b"PK")) else parse_feed(body)
    if feed is not None:
        podcast = False
        try:
            tree: Any = etree.fromstring(
                body, etree.XMLParser(resolve_entities=False, no_network=True)
            )
            types = tree.xpath(
                '//*[local-name()="enclosure"]/@type | '
                '//*[local-name()="link" and @rel="enclosure"]/@type'
            )
            podcast = bool(types) and all(str(t).lower().startswith("audio/") for t in types)
        except (ValueError, etree.XMLSyntaxError):
            pass
        return XmlReview(
            "feed", tuple(dict.fromkeys(e.url for e in parse_feed_entries(body, url))), (), podcast
        )
    sitemap = parse_sitemap(body)
    return XmlReview(
        "news_sitemap" if sitemap.has_news else "sitemap",
        tuple(dict.fromkeys(e.loc for e in sitemap.entries)),
        tuple(dict.fromkeys(e.loc for e in sitemap.children)),
    )


def transient(result: FetchResult) -> bool:
    """Permite uma repetição somente para falhas transitórias identificadas."""
    if result.status in (502, 503, 504):
        return True
    error = result.error or ""
    return any(
        word in error
        for word in (
            "Timeout",
            "RemoteProtocolError",
            "ReadError",
            "Connection reset",
            "Temporary failure",
        )
    )
