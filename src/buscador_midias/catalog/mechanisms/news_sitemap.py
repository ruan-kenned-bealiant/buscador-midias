"""Mecanismo de sitemap com metadados Google News, sem restrição de publicação."""

from buscador_midias.catalog.mechanisms.parsing import Document
from buscador_midias.catalog.mechanisms.sitemap import SitemapMechanism


class NewsSitemapMechanism(SitemapMechanism):
    """Exige metadados de notícia em uma lista urlset."""

    id = "news_sitemap"
    label = "Sitemap de notícias"
    description = "Exige metadados Google News; publicação antiga não reprova o link."

    def accepts(self, document: Document) -> bool:
        """Preserva a exigência do formato de notícias sem aplicar filtro de data."""
        return super().accepts(document) and document["news"]
