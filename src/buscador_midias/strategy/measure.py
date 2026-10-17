"""Mede o que um sitemap ou feed publica agora (parte pura, sem rede)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from buscador_midias.domain.strategy import Item, Measure, StrategyKind

RECENT_WINDOW = timedelta(hours=48)
OTHER_SECTION = "(outros)"
MAX_TOP_SECTIONS = 40
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
    recent_items = recent_of(items, now, window)
    recent = len(recent_items)
    counts = Counter(item.section for item in (recent_items or items))
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return Measure(
        kind=kind,
        items=len(items),
        dated=len(dates),
        recent=recent,
        titled=sum(1 for item in items if item.title),
        newest=max(dates) if dates else None,
        size_bytes=size_bytes,
        sections=len(ranked),
        top_sections=tuple(ranked[:MAX_TOP_SECTIONS]),
    )


def recent_of(
    items: Sequence[Item], now: datetime, window: timedelta = RECENT_WINDOW
) -> list[Item]:
    """As matérias publicadas na janela recente (datas além da tolerância de fuso não contam)."""
    return [
        item
        for item in items
        if item.at is not None and now - window <= item.at <= now + FUTURE_TOLERANCE
    ]


def section_of(url: str, prefix: str = "") -> str:
    """A seção do site a que a URL pertence: o primeiro segmento do caminho, sem o prefixo da fonte.

    Datas, números e nomes de arquivo (``2026``, ``12345``, ``materia.html``) não são seção, nem um
    caminho de um segmento só (``/titulo-da-materia``: o segmento é a própria matéria).

    Args:
        url: A URL da matéria.
        prefix: Prefixo de seção da própria fonte (``/globonews``), que não conta.

    Returns:
        A seção em minúsculas, ou ``(outros)`` se a URL não traz uma.
    """
    path = urlsplit(url).path
    if prefix and path.lower().startswith(prefix.lower()):
        path = path[len(prefix) :]
    parts = [part for part in path.split("/") if part]
    # Com um segmento só, ele é o título da matéria (URL plana), não uma seção.
    segment = parts[0].lower() if len(parts) > 1 else ""
    if not segment or "." in segment or re.fullmatch(r"[\d_-]+", segment):
        return OTHER_SECTION
    return segment
