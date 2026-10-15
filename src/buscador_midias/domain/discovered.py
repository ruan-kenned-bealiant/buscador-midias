"""Modelo de uma URL descoberta em um sitemap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from buscador_midias.domain.sitemap import SitemapEntry
from buscador_midias.domain.url import url_hash


@dataclass(frozen=True, slots=True)
class DiscoveredUrl:
    """Uma URL de notícia vista em um sitemap, pronta para ser lembrada e comparada.

    Attributes:
        url: URL como veio no sitemap.
        url_hash: Hash da URL canonicalizada (mesma regra do corpus do KyX); é a identidade.
        site_key: Fonte (``Site.key``) a que a URL pertence.
        source_sitemap: Sitemap em que foi encontrada.
        lastmod: Data de modificação declarada, se houver.
        title: Título (só vem de news sitemaps).
        publication_date: Data de publicação (só vem de news sitemaps).
    """

    url: str
    url_hash: str
    site_key: str
    source_sitemap: str
    lastmod: datetime | None
    title: str | None
    publication_date: datetime | None

    @classmethod
    def from_entry(
        cls, entry: SitemapEntry, *, site_key: str, source_sitemap: str
    ) -> DiscoveredUrl:
        """Cria a partir de uma entrada de sitemap, calculando o ``url_hash`` canônico."""
        return cls(
            url=entry.loc,
            url_hash=url_hash(entry.loc),
            site_key=site_key,
            source_sitemap=source_sitemap,
            lastmod=entry.lastmod,
            title=entry.title,
            publication_date=entry.publication_date,
        )

    @property
    def timestamp(self) -> datetime | None:
        """Melhor data disponível: publicação (news sitemap) ou, na falta dela, ``lastmod``."""
        return self.publication_date or self.lastmod
