"""Critérios puros para aplicar provas de mapeamento sem confundir arquivo com atualização."""

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit


def normalized(url: str) -> tuple[str, str, str]:
    """Identifica diferenças cosméticas sem eliminar consultas semanticamente distintas."""
    parts = urlsplit(url)
    return (parts.hostname or "").removeprefix("www."), parts.path.rstrip("/"), parts.query


def recent_samples(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Exige artigo, texto e publicação comprovada nos três dias anteriores ao teste."""
    until = datetime.fromisoformat(result["window_until"])
    since = until - timedelta(days=3)
    found = []
    for sample in result.get("samples", []):
        if not (sample.get("text") and sample.get("article") and sample.get("published_at")):
            continue
        try:
            published = datetime.fromisoformat(sample["published_at"])
            if published.tzinfo is not None and since <= published <= until:
                found.append(sample)
        except ValueError:
            continue
    return found


def purpose(result: dict[str, Any]) -> str:
    """Não transforma uma folha datada ou índice enorme em rota diária eficiente."""
    parts = urlsplit(result["url"])
    if re.search(r"(?:19|20)\d{2}[/_-](?:0[1-9]|1[0-2])", parts.path) or any(
        key.lower() in {"year", "yyyy", "date", "month"} for key in parse_qs(parts.query)
    ):
        return "archive"
    if result.get("root_index") and result.get("root_children", 0) > 100:
        return "updates" if result["model"] == "g1_daily" else "review"
    return "updates"
