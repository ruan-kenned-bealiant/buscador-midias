"""Mecanismo de sitemap direto, inclusive arquivos gzip e zip."""

from buscador_midias.catalog.mechanisms.base import Mechanism
from buscador_midias.catalog.mechanisms.parsing import Document


class SitemapMechanism(Mechanism):
    """Lê listas urlset e não confunde referências de índice com notícias."""

    id = "sitemap"
    label = "Sitemap direto"
    description = "Lê uma lista de notícias (urlset), inclusive XML gzip ou zip."
    kind = "sitemap"

    def accepts(self, document: Document) -> bool:
        """Exige lista de URLs, sem expansão implícita de índices."""
        return document["kind"] == "sitemap" and not document["index"]
