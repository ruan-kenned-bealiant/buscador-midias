"""Mecanismo de navegação por índices aninhados de sitemaps."""

from buscador_midias.catalog.mechanisms.base import COMMON_PARAMETERS, TREE_PARAMETERS, Mechanism
from buscador_midias.catalog.mechanisms.parsing import Document


class SitemapIndexMechanism(Mechanism):
    """Aceita índices e folhas e disponibiliza os filhos ao executor comum."""

    id = "sitemap_tree"
    label = "Índice de sitemaps"
    description = "Segue índices e sitemaps filhos, com deduplicação e até 10 níveis."
    kind = "sitemap"
    expand = True
    parameters = COMMON_PARAMETERS + TREE_PARAMETERS

    def accepts(self, document: Document) -> bool:
        """Aceita tanto um índice como uma folha urlset."""
        return document["kind"] == "sitemap"

    def children(self, document: Document) -> list[str]:
        """Expõe as referências de filhos, sem transformá-las em notícias."""
        return document["children"]
