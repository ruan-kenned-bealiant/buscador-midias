"""Frescor de um link: há quanto tempo o próprio feed ou sitemap deixou de trazer notícia nova.

A data vem do documento que o link serve agora, não do histórico de testes: a cadência guardada
nos testes sai de poucas páginas amostradas e subestima links vivos. Só a data de publicação é
exata; data de atualização e ``lastmod`` são limites superiores (a notícia é, no máximo, tão
nova quanto elas), então só servem para provar que um link está parado, nunca que está vivo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

INACTIVATE_AFTER = timedelta(days=30)
DELETE_AFTER = timedelta(days=365)
# Sites que publicam em horário local sem fuso aparecem algumas horas "no futuro".
FUTURE_TOLERANCE = timedelta(hours=14)
# Datas de preenchimento (1970, 0001) aparecem em feeds sem data real e não provam abandono.
EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Freshness:
    """Data mais nova encontrada, de onde veio e se ela é a data real de publicação."""

    newest: datetime | None
    basis: str
    exact: bool


def _read(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _newest(values: Any, now: datetime) -> datetime | None:
    moments = [_read(value) for value in values]
    return max((m for m in moments if m and EARLIEST <= m <= now + FUTURE_TOLERANCE), default=None)


def document_freshness(document: Any, now: datetime) -> Freshness:
    """Escolhe o sinal mais confiável do documento: publicação, atualização, lastmod do índice."""
    for key, basis, exact in (
        ("dates", "publication", True),
        ("hints", "update", False),
        ("child_dates", "index_lastmod", False),
    ):
        newest = _newest(document[key].values(), now)
        if newest:
            return Freshness(newest, basis, exact)
    return Freshness(None, "none", False)


def link_verdict(found: Freshness, now: datetime) -> str:
    """Mantém, inativa (mais de 1 mês parado), exclui (1 ano ou mais) ou deixa sem veredito."""
    if found.newest is None:
        return "unknown"
    age = now - found.newest
    if age >= DELETE_AFTER:
        return "delete"
    return "inactivate" if age > INACTIVATE_AFTER else "keep"
