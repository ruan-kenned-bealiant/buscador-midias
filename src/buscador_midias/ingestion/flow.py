"""Limites de trabalho em trânsito, independentes do número total de links."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

QUEUE_LIMITS = dict(discovery=64, dedup=1000, fetch=64, context=32, evidence=32, persist=32)
# Evidência pode acumular no PostgreSQL sem pausar a análise de contexto.
# QUEUE_LIMITS continua limitando apenas o lote publicado no RabbitMQ.
# Descoberta e dedup produzem metadados leves no PostgreSQL, sem aguardar downloads.
BACKLOG_LIMITS = dict(context=200, persist=200)
DEDUP_BATCH = 500
RESERVE_BYTES = 256 * 1024 * 1024


def today_start(now: datetime, timezone: str = "America/Sao_Paulo") -> datetime:
    """Calcula a meia-noite civil, sem confundir dia atual com últimas 24 horas."""
    if now.tzinfo is None:
        raise ValueError("O instante precisa conter fuso horário.")
    return now.astimezone(ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0)


MAX_DAY_AGE = 30


def day_window(
    day: str, now: datetime, timezone: str = "America/Sao_Paulo"
) -> tuple[datetime, datetime]:
    """Janela de um dia civil inteiro; o dia corrente termina agora, nunca no futuro.

    O limite de 30 dias evita varrer arquivos antigos, cujos feeds já não listam o dia.
    """
    try:
        civil = date.fromisoformat(day)
    except (TypeError, ValueError):
        raise ValueError("Informe o dia no formato AAAA-MM-DD.") from None
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    if civil > today:
        raise ValueError("O dia não pode estar no futuro.")
    if (today - civil).days > MAX_DAY_AGE:
        raise ValueError(f"O dia deve estar nos últimos {MAX_DAY_AGE} dias.")
    since = datetime.combine(civil, time.min, zone)
    return since, min(datetime.combine(civil + timedelta(days=1), time.min, zone), now)


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


# Folga contra fuso mal rotulado em ``lastmod``: o instante real pode ser até 12 h posterior.
LASTMOD_MARGIN = timedelta(hours=12)


def lastmod_outside(stamp: str | None, since: datetime) -> dict[str, Any] | None:
    """Exclui só quando ``lastmod`` prova que a publicação foi anterior à janela.

    Uma página não é publicada depois da última modificação, então ``lastmod`` é um limite
    superior da publicação: serve para excluir, nunca para datar a notícia. Data pura
    (meia-noite exata ou sem fuso) vale o dia inteiro, porque o dia pode terminar em outro fuso.
    """
    if not stamp:
        return None
    try:
        modified = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    whole_day = modified.tzinfo is None or modified.timetz().replace(tzinfo=None) == time.min
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=UTC)
    bound = modified + (timedelta(days=1) if whole_day else timedelta()) + LASTMOD_MARGIN
    if bound >= since:
        return None
    return dict(rule="lastmod_before_window", lastmod=stamp, bound=bound.isoformat())


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
