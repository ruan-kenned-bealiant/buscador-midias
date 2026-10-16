"""Modelos da estratégia de descoberta de uma fonte: o que foi sondado e o que foi escolhido."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    """O papel de um candidato escolhido.

    O principal é o que a rodada usa; a reserva cobre a falha dele (outro tipo de documento); o
    complemento acrescenta matérias que os outros não listam (cobertura do site todo).
    """

    PRIMARY = "primary"
    BACKUP = "backup"
    COMPLEMENT = "complement"


@dataclass(frozen=True, slots=True)
class Item:
    """Uma matéria listada por um sitemap ou feed.

    Attributes:
        url: Endereço da matéria.
        at: Melhor data disponível (publicação ou ``lastmod``), em UTC.
        title: Título, se o documento o traz.
        section: Seção do site a que a URL pertence (primeiro segmento do caminho).
    """

    url: str
    at: datetime | None
    title: str | None
    section: str = ""


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
        sections: Quantas seções do site aparecem entre os itens recentes (todos, se nenhum tem
            data): quanto mais, mais o candidato cobre o site inteiro e não só uma parte.
        top_sections: As seções mais frequentes e quantos itens cada uma tem.
    """

    kind: StrategyKind
    items: int
    dated: int
    recent: int
    titled: int
    newest: datetime | None
    size_bytes: int
    sections: int = 0
    top_sections: tuple[tuple[str, int], ...] = ()


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
        coverage: Fração das matérias recentes de todos os candidatos frescos que este lista.
        union_recent: Quantas matérias recentes distintas todos os candidatos frescos listam juntos
            (o "site todo" que dá para ver); é do site, repetido em cada candidato.
        cover_share: Fração dessa união que o principal, a reserva e os complementos juntos cobrem
            (do site, repetido em cada candidato).
        recent_urls: As matérias recentes do candidato, só na memória: servem para calcular a
            cobertura e não são gravadas.
        forced: Se um humano mandou usar este endereço (``estrategias_manuais.csv``); só na
            memória: o papel de principal é o que se grava.
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
    coverage: float | None = None
    union_recent: int = 0
    cover_share: float | None = None
    recent_urls: frozenset[str] = field(default=frozenset(), compare=False, repr=False)
    forced: bool = field(default=False, compare=False)


@dataclass(frozen=True, slots=True)
class RoundHealth:
    """Como a descoberta de uma fonte foi em uma rodada (o que a saúde da estratégia observa).

    Attributes:
        started_at: Quando a rodada começou.
        status: Desfecho da fonte (``ok``, ``blocked``, ``error``...).
        new_urls: URLs novas que a rodada achou.
        seen_urls: URLs que a fonte listou na rodada (as de sitemaps pulados não contam).
        sitemaps_skipped: Sitemaps filhos pulados por ``lastmod`` inalterado; com pulos,
            ``seen_urls`` não é comparável com o de outra rodada.
    """

    started_at: datetime
    status: str
    new_urls: int
    seen_urls: int
    sitemaps_skipped: int


@dataclass(frozen=True, slots=True)
class ProbeState:
    """Quando e por que uma fonte volta a ser sondada.

    Attributes:
        site_key: A fonte.
        probed_at: Última sondagem.
        found: Se a última sondagem achou um principal.
        streak: Sondagens seguidas sem melhora (por saúde sem mudar nada, ou sem achar nada); dobra
            a espera até a próxima reavaliação.
        cooldown_until: Antes disso, a saúde (falha, silêncio, encolhimento) não pede reavaliação.
        refresh_at: Quando a estratégia fica velha e é reavaliada de qualquer jeito; para uma fonte
            sem estratégia, quando tentar de novo.
    """

    site_key: str
    probed_at: datetime
    found: bool
    streak: int
    cooldown_until: datetime
    refresh_at: datetime
    detail: str = ""
