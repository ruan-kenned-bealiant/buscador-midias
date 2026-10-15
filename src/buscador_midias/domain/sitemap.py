"""Modelos de um sitemap já interpretado."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SitemapKind(StrEnum):
    """Tipo do documento: índice de sitemaps ou lista de URLs."""

    INDEX = "index"
    URLSET = "urlset"


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    """Uma URL listada em um ``urlset``.

    Attributes:
        loc: URL da página.
        lastmod: Data de modificação declarada (UTC), se houver e for válida.
        title: Título vindo de um news sitemap, se houver.
        publication_date: Data de publicação vinda de um news sitemap (UTC), se houver.
    """

    loc: str
    lastmod: datetime | None = None
    title: str | None = None
    publication_date: datetime | None = None

    @property
    def timestamp(self) -> datetime | None:
        """Melhor data disponível: publicação (news sitemap) ou, na falta, ``lastmod``."""
        return self.publication_date or self.lastmod


@dataclass(frozen=True, slots=True)
class SitemapRef:
    """Um sitemap filho listado em um ``sitemapindex``.

    Attributes:
        loc: URL do sitemap filho.
        lastmod: Data de modificação declarada (UTC), se houver e for válida.
    """

    loc: str
    lastmod: datetime | None = None


@dataclass(frozen=True, slots=True)
class ParsedSitemap:
    """Resultado da leitura de um sitemap.

    Attributes:
        kind: Se é um índice ou uma lista de URLs.
        entries: URLs (vazio em um índice).
        children: Sitemaps filhos (vazio em um ``urlset``).
        has_news: Se alguma URL traz metadados de news sitemap.
    """

    kind: SitemapKind
    entries: tuple[SitemapEntry, ...] = ()
    children: tuple[SitemapRef, ...] = ()
    has_news: bool = False
