"""Limites de trabalho em trânsito, independentes do número total de links."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

QUEUE_LIMITS = dict(discovery=64, dedup=1000, fetch=64, context=32, evidence=32, persist=32)
BACKLOG_LIMITS = dict(dedup=5000, fetch=2000, context=200, evidence=200, persist=200)
DEDUP_BATCH = 100
RESERVE_BYTES = 256 * 1024 * 1024


def today_start(now: datetime, timezone: str = "America/Sao_Paulo") -> datetime:
    """Calcula a meia-noite civil, sem confundir dia atual com últimas 24 horas."""
    if now.tzinfo is None:
        raise ValueError("O instante precisa conter fuso horário.")
    return now.astimezone(ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0)


def clock_slot(now: datetime, timezone: str, hours: list[int]) -> datetime | None:
    """Retorna o horário atual com tolerância de 15 minutos, sem repor rodadas passadas."""
    midnight = today_start(now, timezone)
    due = [
        midnight + timedelta(hours=hour)
        for hour in hours
        if midnight + timedelta(hours=hour) <= now
    ]
    slot = max(due) if due else None
    return slot if slot and now - slot < timedelta(minutes=15) else None


def publication_outside(job: dict[str, Any]) -> dict[str, Any] | None:
    """Filtra apenas publicação explícita de RSS/News, nunca lastmod ou data do caminho."""
    stamp = job["article"]["payload"].get("published_hint")
    basis = job["article"]["payload"].get("published_hint_basis")
    if not stamp or basis != "xml_publication":
        return None
    try:
        published = datetime.fromisoformat(stamp)
        if published.tzinfo is None:
            return None
        if job["run"]["since"] <= published <= job["run"]["until"]:
            return None
    except (ValueError, TypeError):
        return None
    return dict(
        outcome="fora_da_janela_no_xml",
        status="outside_window",
        article=dict(published_at=published.isoformat(), date_basis="xml_publication"),
        metrics=dict(avoided_article_requests=1),
    )
