"""Inventário de páginas: exploração da estrutura, independente da coleta de notícias."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PageStatus(StrEnum):
    """Estado de exploração; uma página lida ainda exige validação de cobertura."""

    PENDING = "pending"
    OK = "ok"
    BLOCKED = "blocked"
    ERROR = "error"
    DISALLOWED = "disallowed"
    INVALID = "invalid"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class MapPage:
    """Página conhecida, sua primeira origem e os mecanismos declarados nela.

    ``kind`` distingue a entrada (root), candidatas a seção (section) e taxonomias
    (taxonomy). Links de matéria são amostras estruturais, sem análise ou corpus.
    Feeds são endereços encontrados, ainda sem validação do conteúdo ou da cobertura.
    """

    url: str
    found_on: str
    kind: str
    status: PageStatus = PageStatus.PENDING
    title: str = ""
    article_urls: tuple[str, ...] = ()
    feeds: tuple[str, ...] = ()
    feed_indexes: tuple[str, ...] = ()
    detail: str = ""
    attempts: int = 0
    checked_at: datetime | None = None
    page_type: str = "unknown"
    type_basis: str = ""
    discovery_basis: str = "url_shape"

    @property
    def is_section_candidate(self) -> bool:
        """Se a página ainda pode fazer parte da estrutura editorial do site."""
        return self.kind in ("root", "section") and self.page_type not in (
            "article",
            "service",
            "taxonomy",
        )


@dataclass(frozen=True, slots=True)
class MapSummary:
    """Progresso da exploração; esgotar a fila não prova cobertura de notícias."""

    site_key: str
    sections: int
    visited: int
    pending: int
    issues: int
    excluded: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class MapSource:
    """Leitura auditável de um mecanismo; os links permitem conferir amostras de seção."""

    url: str
    verdict: str
    kind: str | None
    listed_urls: tuple[str, ...]
    checked_at: datetime
    requests: int = 0
    partial: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SourceContribution:
    """Contribuição incremental de um mecanismo na amostra observada da seção."""

    source: MapSource
    matched: int
    added: int


@dataclass(frozen=True, slots=True)
class SectionCoverage:
    """Correspondência observada na amostra; nunca representa cobertura total do site."""

    status: str
    sampled: int
    matched: int
    sources: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    suggested: tuple[SourceContribution, ...] = ()
