"""Mede o que um sitemap ou feed publica agora (parte pura, sem rede)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from buscador_midias.domain.strategy import Item, Measure, StrategyKind

RECENT_WINDOW = timedelta(hours=48)
# Sites que publicam em horário local sem fuso aparecem algumas horas "no futuro".
FUTURE_TOLERANCE = timedelta(hours=14)


def measure_items(
    kind: StrategyKind,
    items: Sequence[Item],
    *,
    now: datetime,
    size_bytes: int,
    window: timedelta = RECENT_WINDOW,
) -> Measure:
    """Conta quantas matérias a lista traz, quantas têm data, título e são recentes.

    Args:
        kind: O tipo de documento medido.
        items: As matérias da fonte (já sem as de outros domínios).
        now: Instante de referência.
        size_bytes: Bytes baixados para obter a lista.
        window: Janela em que uma data conta como recente.

    Returns:
        A medida. Datas além da tolerância de fuso para o futuro não contam como recentes.
    """
    dates = [item.at for item in items if item.at is not None]
    recent = sum(1 for at in dates if now - window <= at <= now + FUTURE_TOLERANCE)
    return Measure(
        kind=kind,
        items=len(items),
        dated=len(dates),
        recent=recent,
        titled=sum(1 for item in items if item.title),
        newest=max(dates) if dates else None,
        size_bytes=size_bytes,
    )
