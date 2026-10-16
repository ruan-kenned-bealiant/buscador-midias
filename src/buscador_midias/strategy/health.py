"""Quando reavaliar a estratégia de uma fonte (regras explícitas sobre o que as rodadas mostram)."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from buscador_midias.domain.strategy import ProbeState, RoundHealth


class DueReason(StrEnum):
    """Por que uma fonte está na vez de ser sondada."""

    NEVER_PROBED = "never_probed"
    RETRY = "retry"
    REFRESH = "refresh"
    FAILING = "failing"
    SILENT = "silent"
    SHRUNK = "shrunk"


@dataclass(frozen=True, slots=True)
class HealthPolicy:
    """Os parâmetros da saúde, num lugar só.

    Attributes:
        failure_rounds: Rodadas seguidas com falha para reavaliar.
        silence_rounds: Rodadas seguidas ``ok`` sem URL nova para reavaliar...
        min_history_new: ...numa fonte que antes rendeu pelo menos isto (senão é uma fonte quieta).
        min_history_rounds: Rodadas anteriores necessárias para comparar o volume listado.
        min_history_seen: Mediana mínima de URLs listadas para a comparação valer (fonte pequena
            oscila demais).
        shrink_ratio: O volume listado abaixo desta fração da mediana é encolhimento (feed ou
            sitemap que passou a mostrar só uma parte).
        refresh_days: Idade em que a estratégia é reavaliada de qualquer jeito (endereços mudam).
        backoff_base_hours: Primeira espera depois de uma sondagem sem melhora; dobra a cada uma.
        backoff_max_days: Teto da espera.
    """

    failure_rounds: int = 2
    silence_rounds: int = 4
    min_history_new: int = 10
    min_history_rounds: int = 3
    min_history_seen: int = 20
    shrink_ratio: float = 0.25
    refresh_days: int = 7
    backoff_base_hours: int = 6
    backoff_max_days: int = 7


def backoff(streak: int, policy: HealthPolicy) -> timedelta:
    """Espera depois de ``streak`` sondagens sem melhora: 6 h, 12 h, 24 h... até o teto."""
    hours = policy.backoff_base_hours * 2 ** max(streak - 1, 0)
    return min(timedelta(hours=hours), timedelta(days=policy.backoff_max_days))


def adopt(site_key: str, probed_at: datetime, policy: HealthPolicy) -> ProbeState:
    """Estado de uma fonte que já tinha estratégia gravada antes de existir o controle de saúde."""
    return ProbeState(
        site_key=site_key,
        probed_at=probed_at,
        found=True,
        streak=0,
        cooldown_until=probed_at,
        refresh_at=probed_at + timedelta(days=policy.refresh_days),
    )


def due_reason(
    state: ProbeState | None,
    rounds: Sequence[RoundHealth],
    *,
    now: datetime,
    policy: HealthPolicy,
) -> DueReason | None:
    """Diz se a fonte está na vez de ser sondada e por quê.

    A ordem é a da certeza: nunca sondada, nova tentativa de quem não tinha estratégia, idade e, só
    fora da espera de saúde, o que as rodadas desde a última sondagem mostram.

    Args:
        state: O estado da fonte (``None`` se nunca foi sondada).
        rounds: As rodadas da fonte, da mais nova para a mais antiga.
        now: Instante de referência.
        policy: Parâmetros da saúde.

    Returns:
        O motivo, ou ``None`` se a fonte está em dia.
    """
    if state is None:
        return DueReason.NEVER_PROBED
    if now >= state.refresh_at:
        return DueReason.RETRY if not state.found else DueReason.REFRESH
    if not state.found or now < state.cooldown_until:
        return None
    since = [r for r in rounds if r.started_at > state.probed_at]
    if _failing(since, policy):
        return DueReason.FAILING
    if _silent(since, rounds, policy):
        return DueReason.SILENT
    if _shrunk(since, policy):
        return DueReason.SHRUNK
    return None


def next_state(
    previous: ProbeState | None,
    site_key: str,
    *,
    now: datetime,
    found: bool,
    changed: bool,
    reason: DueReason | None,
    policy: HealthPolicy,
) -> ProbeState:
    """O estado depois de uma sondagem.

    Sem achar nada, ou reavaliando por saúde sem mudar a escolha, a espera até a próxima
    reavaliação por saúde dobra: uma fonte que é quieta por natureza não é sondada a cada rodada.

    Args:
        previous: O estado antes (``None`` na primeira vez).
        site_key: A fonte.
        now: Instante da sondagem.
        found: Se a sondagem achou um principal.
        changed: Se o principal escolhido é outro que o de antes.
        reason: Por que a sondagem aconteceu (``None`` quando foi manual).
        policy: Parâmetros da saúde.

    Returns:
        O novo estado.
    """
    if not found:
        streak = (previous.streak if previous else 0) + 1
        wait = backoff(streak, policy)
        return ProbeState(site_key, now, False, streak, now + wait, now + wait)
    by_health = reason in (DueReason.FAILING, DueReason.SILENT, DueReason.SHRUNK)
    streak = previous.streak + 1 if previous and by_health and not changed else 0
    return ProbeState(
        site_key,
        now,
        True,
        streak,
        now + backoff(streak, policy) if streak else now,
        now + timedelta(days=policy.refresh_days),
    )


def _ok(round_: RoundHealth) -> bool:
    return round_.status == "ok"


def _failing(since: Sequence[RoundHealth], policy: HealthPolicy) -> bool:
    """As últimas rodadas desde a sondagem falharam todas."""
    window = since[: policy.failure_rounds]
    return len(window) == policy.failure_rounds and not any(_ok(r) for r in window)


def _silent(
    since: Sequence[RoundHealth], rounds: Sequence[RoundHealth], policy: HealthPolicy
) -> bool:
    """Várias rodadas ok sem nada novo numa fonte que antes rendia."""
    size = policy.silence_rounds
    window = since[:size]
    if len(window) < size or not all(_ok(r) and r.new_urls == 0 for r in window):
        return False
    return sum(r.new_urls for r in rounds[size:] if _ok(r)) >= policy.min_history_new


def _shrunk(since: Sequence[RoundHealth], policy: HealthPolicy) -> bool:
    """A última rodada listou muito menos que o de costume (só se a comparação é justa).

    A base são só as rodadas desde a última sondagem: antes dela a fonte era lida de outro jeito, e
    o que "listar" significava (ex.: sitemaps de arquivo) não é comparável. Rodadas que pularam
    sitemaps filhos também não entram.
    """
    if not since or not _ok(since[0]) or since[0].sitemaps_skipped:
        return False
    prior = [r.seen_urls for r in since[1:] if _ok(r) and not r.sitemaps_skipped]
    if len(prior) < policy.min_history_rounds:
        return False
    median = statistics.median(prior)
    return median >= policy.min_history_seen and since[0].seen_urls < policy.shrink_ratio * median
