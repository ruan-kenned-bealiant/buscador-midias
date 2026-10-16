"""Modelo de um item de feed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """Um item de feed: a matéria que o site acabou de publicar.

    Attributes:
        url: Endereço absoluto da matéria.
        title: Título (vazio se o item não tem).
        published: Data de publicação (UTC), ou ``None`` se o item não tem data legível.
    """

    url: str
    title: str
    published: datetime | None
