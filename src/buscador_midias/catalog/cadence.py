"""Estimativa de publicação por amostra; lastmod de sitemap não é data de publicação."""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any


def estimate_cadence(dates: dict[str, datetime]) -> dict[str, Any]:
    """Resume datas por URL sem confundir observação única com frequência garantida."""
    ordered = sorted(dates.values())
    base = dict(
        status="unknown",
        samples=len(ordered),
        confidence="insufficient",
        method="publication_dates_sample",
        interval_seconds=None,
    )
    if ordered:
        base.update(oldest=ordered[0].isoformat(), newest=ordered[-1].isoformat())
    if len(ordered) < 3 or ordered[-1] == ordered[0]:
        return base
    gaps = [(b - a).total_seconds() for a, b in zip(ordered, ordered[1:], strict=False)]
    positive = [gap for gap in gaps if gap > 0]
    span = (ordered[-1] - ordered[0]).total_seconds()
    interval = median(positive) if positive else None
    base.update(
        status="estimated",
        interval_seconds=interval,
        observed_per_day=round((len(ordered) - 1) * 86400 / span, 4),
        confidence="medium" if len(ordered) >= 20 and span >= 7 * 86400 else "low",
        note="Estimativa da amostra; não é agenda garantida. Datas RSS podem ser atualização.",
    )
    return base
