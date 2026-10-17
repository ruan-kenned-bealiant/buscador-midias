"""Veredito, nota e escolha das estratégias de uma fonte (regras explícitas, sem aprendizado)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from buscador_midias.domain.strategy import (
    CandidateReport,
    Measure,
    Role,
    StrategyKind,
    Verdict,
)
from buscador_midias.strategy.measure import FUTURE_TOLERANCE, OTHER_SECTION, section_of

_SCORED = frozenset({Verdict.FRESH, Verdict.UNDATED, Verdict.SLOW})


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    """Os parâmetros do julgamento, num lugar só.

    Attributes:
        archive_min_items: Acima deste tamanho, uma lista quase sem itens recentes é arquivo (ler
            tudo custa muito e só traz o passado).
        archive_max_recent_share: Fração de recentes abaixo da qual uma lista grande é arquivo...
        archive_keep_recent: ...a menos que tenha ao menos este número de recentes: um sitemap de
            10 mil URLs com centenas de recentes está vivo, mesmo que sejam poucos por cento.
        coverage_cap: Teto de itens recentes que contam na nota (mais que isso não melhora a fonte).
        undated_coverage: Cobertura atribuída a uma lista sem datas (não dá para provar frescor).
        slow_window_days: Uma fonte sem nada nas últimas 48 h, mas com matéria neste prazo, é lenta
            (publica devagar), não defasada: a janela de 48 h é só uma régua da POC, e o objetivo é
            mapear como obter as matérias de cada fonte, também das que publicam de vez em quando.
        slow_coverage: Cobertura atribuída a uma fonte lenta: abaixo de qualquer fonte fresca (que
            tem ao menos uma matéria recente) e acima de uma lista sem datas, que nem a vida prova.
        untitled_factor: Multiplicador de uma lista em que menos da metade dos itens tem título.
        max_complements: Quantas fontes a mais, além do principal e da reserva, entram para cobrir o
            que eles não listam (alto: um site de muitas seções precisa de uma fonte por seção).
        min_gain_urls: Matérias novas mínimas que uma fonte precisa acrescentar para virar
            complemento...
        min_gain_share: ...e a fração mínima da união (vale o maior dos dois): uma fonte que traz
            uma ou duas matérias a mais não paga outra leitura por rodada.
        min_section_gain_urls: Exceção: uma fonte que traz uma **seção nova** (que nada do que já
            foi escolhido lista) vira complemento com este mínimo de matérias novas, por poucas
            que sejam em relação ao site todo.
        kind_factor: Peso de cada tipo; o sitemap de notícias é o mais confiável e o genérico o
            menos (não traz título, e ``lastmod`` muda quando a matéria é só editada); a varredura
            de listagens é o último recurso (não traz data nem título).
    """

    archive_min_items: int = 5000
    archive_max_recent_share: float = 0.05
    archive_keep_recent: int = 100
    coverage_cap: int = 1000
    undated_coverage: float = 0.5
    slow_window_days: int = 90
    slow_coverage: float = 0.75
    untitled_factor: float = 0.9
    max_complements: int = 30
    min_section_gain_urls: int = 2
    min_gain_urls: int = 5
    min_gain_share: float = 0.05
    kind_factor: dict[StrategyKind, float] = field(
        default_factory=lambda: {
            StrategyKind.NEWS_SITEMAP: 1.0,
            StrategyKind.FEED: 0.9,
            StrategyKind.SITEMAP: 0.6,
            StrategyKind.LISTING: 0.5,
        }
    )


def judge(
    measure: Measure, policy: ScoringPolicy | None = None, *, now: datetime | None = None
) -> Verdict:
    """Conclui se a lista serve para descobrir matérias novas.

    Args:
        measure: O que o candidato publica.
        policy: Parâmetros do julgamento.
        now: Instante de referência; sem ele não se distingue a fonte lenta da defasada.

    Returns:
        ``EMPTY`` sem itens; ``ARCHIVE`` para lista enorme quase sem recentes (ou sem datas);
        ``UNDATED`` sem nenhuma data; ``FRESH`` se há matéria recente; ``SLOW`` se a mais nova tem
        até ``slow_window_days``; senão ``STALE``.
    """
    policy = policy or ScoringPolicy()
    if measure.items == 0:
        return Verdict.EMPTY
    if (
        measure.items >= policy.archive_min_items
        and measure.recent / measure.items < policy.archive_max_recent_share
        and measure.recent < policy.archive_keep_recent
    ):
        return Verdict.ARCHIVE
    if measure.dated == 0:
        return Verdict.UNDATED
    if measure.recent == 0:
        return Verdict.SLOW if _is_slow(measure, policy, now) else Verdict.STALE
    return Verdict.FRESH


def _is_slow(measure: Measure, policy: ScoringPolicy, now: datetime | None) -> bool:
    """Se a matéria mais nova é de dentro do prazo da fonte lenta (e não é de um futuro absurdo)."""
    if now is None or measure.newest is None:
        return False
    age = now - measure.newest
    return -FUTURE_TOLERANCE <= age <= timedelta(days=policy.slow_window_days)


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
    coverage = {
        Verdict.FRESH: float(min(measure.recent, policy.coverage_cap)),
        Verdict.SLOW: policy.slow_coverage,
    }.get(verdict, policy.undated_coverage)
    richness = 1.0 if measure.titled * 2 >= measure.items else policy.untitled_factor
    return coverage * policy.kind_factor[measure.kind] * richness


def choose(reports: Sequence[CandidateReport]) -> tuple[CandidateReport, ...]:
    """Marca o principal (maior nota, ou o forçado a mão) e a reserva (a melhor de outro tipo).

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
        primary = next((r for r in usable if r.forced), usable[0])
        roles[primary.url] = Role.PRIMARY
        backup = next(
            (r for r in usable if r.url != primary.url and r.kind is not primary.kind), None
        )
        if backup is not None:
            roles[backup.url] = Role.BACKUP
    return tuple(replace(r, role=roles.get(r.url)) for r in reports)


def plan_coverage(
    reports: Sequence[CandidateReport], policy: ScoringPolicy | None = None
) -> tuple[CandidateReport, ...]:
    """Mede a cobertura do site e escolhe complementos para o que o principal não lista.

    A união das matérias recentes dos candidatos frescos é o "site todo" que dá para ver. Cada
    candidato recebe a fração dela que lista (``coverage``). Depois, partindo do que o principal e a
    reserva já cobrem, escolhem-se complementos, um a um: sempre a fonte que acrescenta mais
    matérias ainda não cobertas, enquanto acrescentar o bastante (``min_gain_*``) e até
    ``max_complements``. O resultado diz quanto da união o conjunto escolhido cobre
    (``cover_share``).

    Args:
        reports: Os candidatos, já com principal e reserva (``choose``).
        policy: Parâmetros do julgamento.

    Returns:
        Os mesmos candidatos, na mesma ordem, com cobertura, união e, nos complementos, o papel.
        Sem nenhum candidato fresco, nada muda.
    """
    policy = policy or ScoringPolicy()
    fresh = [r for r in reports if r.verdict is Verdict.FRESH and r.recent_urls]
    union: set[str] = set().union(*(r.recent_urls for r in fresh)) if fresh else set()
    if not union:
        return tuple(reports)
    covered: set[str] = set()
    for report in reports:
        if report.role in (Role.PRIMARY, Role.BACKUP):
            covered |= report.recent_urls
    roles = {r.url: r.role for r in reports}
    remaining = [r for r in fresh if r.role is None]
    threshold = max(policy.min_gain_urls, policy.min_gain_share * len(union))
    seen_sections = {section_of(url) for url in covered} - {OTHER_SECTION}

    def worth(report: CandidateReport) -> bool:
        gain = len(report.recent_urls - covered)
        if gain >= threshold:
            return True
        fresh_sections = {section_of(u) for u in report.recent_urls - covered}
        new_section = bool(fresh_sections - seen_sections - {OTHER_SECTION})
        return new_section and gain >= policy.min_section_gain_urls

    complements = 0
    while remaining and complements < policy.max_complements:
        candidates = [r for r in remaining if worth(r)]
        if not candidates:
            break
        best = min(candidates, key=lambda r: (-len(r.recent_urls - covered), -r.score, r.url))
        roles[best.url] = Role.COMPLEMENT
        covered |= best.recent_urls
        seen_sections |= {section_of(u) for u in best.recent_urls} - {OTHER_SECTION}
        remaining.remove(best)
        complements += 1
    share = len(covered & union) / len(union)
    fresh_urls = {r.url for r in fresh}
    return tuple(
        replace(
            r,
            role=roles[r.url],
            coverage=len(r.recent_urls) / len(union) if r.url in fresh_urls else None,
            union_recent=len(union),
            cover_share=share,
        )
        for r in reports
    )
