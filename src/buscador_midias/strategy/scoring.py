"""Veredito, nota e escolha das estratégias de uma fonte (regras explícitas, sem aprendizado)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from buscador_midias.domain.strategy import (
    CandidateReport,
    Measure,
    Role,
    StrategyKind,
    Verdict,
)

_SCORED = frozenset({Verdict.FRESH, Verdict.UNDATED})


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    """Os parâmetros do julgamento, num lugar só.

    Attributes:
        archive_min_items: Acima deste tamanho, uma lista quase sem itens recentes é arquivo (ler
            tudo custa muito e só traz o passado).
        archive_max_recent_share: Fração de recentes abaixo da qual uma lista grande é arquivo.
        coverage_cap: Teto de itens recentes que contam na nota (mais que isso não melhora a fonte).
        undated_coverage: Cobertura atribuída a uma lista sem datas (não dá para provar frescor).
        untitled_factor: Multiplicador de uma lista em que menos da metade dos itens tem título.
        kind_factor: Peso de cada tipo; o sitemap de notícias é o mais confiável e o genérico o
            menos (não traz título, e ``lastmod`` muda quando a matéria é só editada).
    """

    archive_min_items: int = 5000
    archive_max_recent_share: float = 0.05
    coverage_cap: int = 1000
    undated_coverage: float = 0.5
    untitled_factor: float = 0.9
    kind_factor: dict[StrategyKind, float] = field(
        default_factory=lambda: {
            StrategyKind.NEWS_SITEMAP: 1.0,
            StrategyKind.FEED: 0.9,
            StrategyKind.SITEMAP: 0.6,
        }
    )


def judge(measure: Measure, policy: ScoringPolicy | None = None) -> Verdict:
    """Conclui se a lista serve para descobrir matérias novas.

    Args:
        measure: O que o candidato publica.
        policy: Parâmetros do julgamento.

    Returns:
        ``EMPTY`` sem itens; ``ARCHIVE`` para lista enorme quase sem recentes (ou sem datas);
        ``UNDATED`` sem nenhuma data; ``STALE`` se nada é recente; senão ``FRESH``.
    """
    policy = policy or ScoringPolicy()
    if measure.items == 0:
        return Verdict.EMPTY
    if (
        measure.items >= policy.archive_min_items
        and measure.recent / measure.items < policy.archive_max_recent_share
    ):
        return Verdict.ARCHIVE
    if measure.dated == 0:
        return Verdict.UNDATED
    if measure.recent == 0:
        return Verdict.STALE
    return Verdict.FRESH


def score(measure: Measure, verdict: Verdict, policy: ScoringPolicy | None = None) -> float:
    """Nota de um candidato: cobertura recente × peso do tipo × riqueza (título).

    Args:
        measure: O que o candidato publica.
        verdict: O veredito do candidato.
        policy: Parâmetros do julgamento.

    Returns:
        0 para o que não serve (só ``FRESH`` e ``UNDATED`` pontuam).
    """
    policy = policy or ScoringPolicy()
    if verdict not in _SCORED:
        return 0.0
    coverage = (
        min(measure.recent, policy.coverage_cap)
        if verdict is Verdict.FRESH
        else policy.undated_coverage
    )
    richness = 1.0 if measure.titled * 2 >= measure.items else policy.untitled_factor
    return coverage * policy.kind_factor[measure.kind] * richness


def choose(reports: Sequence[CandidateReport]) -> tuple[CandidateReport, ...]:
    """Marca o principal (maior nota) e a reserva (a melhor de outro tipo de documento).

    Outro tipo, porque o que se quer da reserva é sobreviver à falha de um formato (sitemap
    quebrado, feed removido), não repetir a mesma aposta.

    Args:
        reports: Os candidatos sondados.

    Returns:
        Os mesmos candidatos, na mesma ordem, com ``role`` preenchido nos escolhidos. Empates de
        nota se resolvem pela URL, para o resultado ser o mesmo a cada rodada.
    """
    usable = sorted((r for r in reports if r.score > 0), key=lambda r: (-r.score, r.url))
    roles: dict[str, Role] = {}
    if usable:
        primary = usable[0]
        roles[primary.url] = Role.PRIMARY
        backup = next((r for r in usable[1:] if r.kind is not primary.kind), None)
        if backup is not None:
            roles[backup.url] = Role.BACKUP
    return tuple(replace(r, role=roles.get(r.url)) for r in reports)
