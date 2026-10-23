"""Mecanismo de RSS 2.0, Atom e RSS 1.0/RDF."""

from buscador_midias.catalog.mechanisms.base import Mechanism
from buscador_midias.catalog.mechanisms.parsing import Document


class RssMechanism(Mechanism):
    """Lê entradas de feed sem percorrer índices de sitemap."""

    id = "rss"
    label = "RSS / Atom / RDF"
    description = "Lê as entradas do feed e confere amostras das notícias."
    kind = "feed"

    def accepts(self, document: Document) -> bool:
        """Um sitemap válido é incompatível com o modelo de feed."""
        return document["kind"] == "feed"
