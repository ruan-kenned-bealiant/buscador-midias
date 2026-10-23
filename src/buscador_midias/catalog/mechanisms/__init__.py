"""Registro explícito de mecanismos; cada comportamento possui um arquivo próprio."""

from buscador_midias.catalog.mechanisms.automatic import AutomaticMechanism
from buscador_midias.catalog.mechanisms.base import Mechanism
from buscador_midias.catalog.mechanisms.news_sitemap import NewsSitemapMechanism
from buscador_midias.catalog.mechanisms.rss import RssMechanism
from buscador_midias.catalog.mechanisms.sitemap import SitemapMechanism
from buscador_midias.catalog.mechanisms.sitemap_index import SitemapIndexMechanism
from buscador_midias.catalog.store_errors import CatalogError

MECHANISMS: dict[str, Mechanism] = {
    m.id: m
    for m in (
        AutomaticMechanism(),
        RssMechanism(),
        SitemapMechanism(),
        SitemapIndexMechanism(),
        NewsSitemapMechanism(),
    )
}


def get_mechanism(value: object, kind: str | None = None) -> Mechanism:
    """Resolve o mecanismo e valida sua associação ao tipo do endpoint."""
    if not isinstance(value, str) or value not in MECHANISMS:
        raise CatalogError("Selecione um modelo de leitura válido.")
    mechanism = MECHANISMS[value]
    if kind is not None and mechanism.kind not in (None, kind):
        raise CatalogError("O modelo selecionado não corresponde ao tipo RSS/sitemap.")
    return mechanism
