"""Regras da rotina de saúde: decisão sobre cada link e agenda por horários fixos.

A decisão parte do resultado da sonda (``link_review``) e do número de verificações seguidas em
que o link falhou. Falha definitiva (404, 410, redirecionamento em ciclo) inativa na hora; falha
que costuma passar (timeout, recusa, 5xx, documento ilegível) só inativa na segunda seguida, para
um site com um dia ruim não sair da rodada. Frescor, período encerrado e tema inútil vêm da
própria sonda, as mesmas regras da varredura do catálogo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Sao_Paulo")
DEFINITIVE_STATUS = (404, 410)
# Quantas verificações seguidas com falha passageira até o link ser inativado.
FAILURES_TO_INACTIVATE = 2


@dataclass(frozen=True)
class Decision:
    """O que fazer com o link: ``keep``, ``inactivate``, ``delete`` ou ``review`` (nada muda)."""

    action: str
    category: str
    reason: str
    failures: int


def _gone_for_good(outcome: dict[str, Any]) -> bool:
    return outcome.get("status") in DEFINITIVE_STATUS or "redirect_cycle" in outcome.get(
        "reason", ""
    )


def decide(outcome: dict[str, Any], previous_failures: int) -> Decision:
    """Traduz a proposta da sonda em ação, contando falhas consecutivas só de acesso e leitura."""
    proposal = outcome["proposal"]
    reason = outcome.get("reason", "")
    if proposal == "keep":
        return Decision("keep", "healthy", "", 0)
    if proposal == "inactivate":
        return Decision("inactivate", "stale", f"parado há {outcome.get('age_days')} dias", 0)
    if proposal in ("delete", "remove_theme", "remove_historical"):
        return Decision("delete", "removed", reason or "parado há 1 ano ou mais", 0)
    cause = outcome.get("cause")
    if cause not in ("access", "unreadable"):
        return Decision("review", "review", reason, 0)
    failures = previous_failures + 1
    category = "access_error" if cause == "access" else "invalid"
    if _gone_for_good(outcome) or failures >= FAILURES_TO_INACTIVATE:
        return Decision("inactivate", category, reason, failures)
    return Decision("review", category, reason, failures)


def next_run(now: datetime, hours: list[int], interval_minutes: int) -> datetime:
    """Próximo horário fixo (em Brasília) depois de ``now``; sem horários, soma o intervalo."""
    if not hours:
        return now + timedelta(minutes=interval_minutes)
    local = now.astimezone(ZONE)
    for day in (0, 1):
        for hour in sorted(hours):
            slot = (local + timedelta(days=day)).replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            if slot > local:
                return slot.astimezone(UTC)
    raise ValueError("Horários inválidos.")  # pragma: no cover - ``hours`` não vazio sempre acha
