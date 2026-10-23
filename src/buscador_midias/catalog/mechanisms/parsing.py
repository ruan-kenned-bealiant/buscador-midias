"""Interpretação XML compartilhada; regras de aceitação ficam em cada mecanismo."""

from typing import TypedDict

from buscador_midias.catalog.compression import MAX_BYTES, unpack_gzip
from buscador_midias.discovery.feeds import parse_feed, parse_feed_entries
from buscador_midias.discovery.sitemap_parser import parse_sitemap
from buscador_midias.domain.sitemap import SitemapKind


class Document(TypedDict):
    """Documento interpretado, mantendo filhos de índice separados de notícias."""

    kind: str
    entries: list[str]
    children: list[str]
    index: bool
    news: bool
    dates: dict[str, str]
    child_dates: dict[str, str]
    hints: dict[str, str]


def parse_document(body: bytes, url: str, max_bytes: int = MAX_BYTES) -> Document:
    """Identifica XML válido sem confundir incompatibilidade de modelo com XML inválido."""
    if body.startswith(b"\x1f\x8b"):
        body = unpack_gzip(body, max_bytes)
    if len(body) > max_bytes:
        raise ValueError(f"XML acima de {max_bytes // (1024 * 1024)} MiB")
    if not body.startswith(b"PK") and parse_feed(body) is not None:
        feed_entries = parse_feed_entries(body, url)
        return Document(
            kind="feed",
            entries=list(dict.fromkeys(e.url for e in feed_entries)),
            children=[],
            index=False,
            news=False,
            dates={e.url: e.published.isoformat() for e in feed_entries if e.published is not None},
            child_dates={},
            hints={e.url: e.published.isoformat() for e in feed_entries if e.published is not None},
        )
    parsed = parse_sitemap(body, max_bytes=max_bytes)
    return Document(
        kind="sitemap",
        entries=[e.loc for e in parsed.entries],
        children=[e.loc for e in parsed.children],
        index=parsed.kind == SitemapKind.INDEX,
        news=parsed.has_news,
        dates={
            e.loc: e.publication_date.isoformat()
            for e in parsed.entries
            if e.publication_date is not None
        },
        hints={e.loc: e.timestamp.isoformat() for e in parsed.entries if e.timestamp is not None},
        child_dates={
            e.loc: e.lastmod.isoformat() for e in parsed.children if e.lastmod is not None
        },
    )
