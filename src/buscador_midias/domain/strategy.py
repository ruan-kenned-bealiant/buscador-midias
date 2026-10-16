"""Modelos da estratégia de descoberta de uma fonte: o que foi sondado e o que foi escolhido."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class StrategyKind(StrEnum):
    """Que tipo de documento lista as matérias recentes de uma fonte."""

    NEWS_SITEMAP = "news_sitemap"
    SITEMAP = "sitemap"
    FEED = "feed"


class CandidateOrigin(StrEnum):
    """De onde veio o endereço candidato."""

    HINT = "hint"
    ROBOTS = "robots"
    COMMON_PATH = "common_path"
    HOME_LINK = "home_link"


class Verdict(StrEnum):
    """O que a sondagem concluiu sobre um candidato."""

    FRESH = "fresh"
    UNDATED = "undated"
    STALE = "stale"
    ARCHIVE = "archive"
    EMPTY = "empty"
    INVALID = "invalid"
    BLOCKED = "blocked"
    DISALLOWED = "disallowed"
    UNAVAILABLE = "unavailable"


class Role(StrEnum):
    """O papel de um candidato escolhido: o que a rodada usa e o que cobre a falha dele."""

    PRIMARY = "primary"
    BACKUP = "backup"


@dataclass(frozen=True, slots=True)
class Item:
    """Uma matéria listada por um sitemap ou feed.

    Attributes:
        url: Endereço da matéria.
        at: Melhor data disponível (publicação ou ``lastmod``), em UTC.
        title: Título, se o documento o traz.
    """

    url: str
    at: datetime | None
    title: str | None


@dataclass(frozen=True, slots=True)
class Measure:
    """O que um candidato publica agora.

    Attributes:
        kind: O tipo de documento.
        items: Matérias da fonte listadas.
        dated: Quantas têm data.
        recent: Quantas foram publicadas na janela recente (48 h).
        titled: Quantas trazem título.
        newest: A data mais recente, ou ``None`` sem nenhuma data.
        size_bytes: Bytes baixados para medir (índice e filhos somados).
    """

    kind: StrategyKind
    items: int
    dated: int
    recent: int
    titled: int
    newest: datetime | None
    size_bytes: int


@dataclass(frozen=True, slots=True)
class CandidateReport:
    """O resultado da sondagem de um endereço candidato.

    Attributes:
        url: O endereço sondado (a entrada: pode ser um índice de sitemaps).
        origin: De onde veio.
        verdict: O que se concluiu.
        kind: O tipo de documento, ``None`` quando não foi possível ler.
        measure: A medida, ``None`` quando não foi possível ler.
        score: A nota (0 quando não serve).
        detail: Explicação curta, em especial das falhas.
        requests: Requisições feitas para sondar (índice e filhos somados).
        role: O papel, se foi escolhido.
    """

    url: str
    origin: CandidateOrigin
    verdict: Verdict
    kind: StrategyKind | None
    measure: Measure | None
    score: float
    detail: str
    requests: int = 0
    role: Role | None = None
