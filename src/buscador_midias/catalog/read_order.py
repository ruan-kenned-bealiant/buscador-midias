"""Ordem em que o documento de um link lista suas entradas, medida uma vez e gravada no link.

Com ``newest_first`` o mecanismo sabe que, ao chegar numa entrada anterior à janela, todas as
seguintes também são e pode parar de ler. A ordem só é afirmada quando o documento inteiro a
confirma; qualquer dúvida (poucas datas, datas iguais, item fixado fora de ordem) vira
``unordered`` ou ``unknown``, que mantêm a leitura completa. Errar para esse lado só custa tempo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

NEWEST_FIRST = "newest_first"
OLDEST_FIRST = "oldest_first"
UNORDERED = "unordered"
UNKNOWN = "unknown"
READ_ORDERS = (NEWEST_FIRST, OLDEST_FIRST, UNORDERED, UNKNOWN)

# Poucas entradas datadas não bastam para afirmar uma ordem.
MIN_DATED = 8
MIN_COVERAGE = 0.8
# Fontes que arredondam ou reordenam por segundos não contam como inversão.
JITTER = timedelta(minutes=1)


def _moment(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def dated_sequence(document: dict[str, Any]) -> list[datetime | None]:
    """Datas na ordem do documento: das entradas, ou do ``lastmod`` dos filhos de um índice."""
    if document["entries"]:
        urls, stamps = document["entries"], {**document["hints"], **document["dates"]}
    else:
        urls, stamps = document["children"], document["child_dates"]
    return [_moment(stamps[u]) if u in stamps else None for u in urls]


def detect_read_order(document: dict[str, Any]) -> str:
    """Classifica o documento já interpretado (saída de ``parse_document``)."""
    sequence = dated_sequence(document)
    dated = [m for m in sequence if m]
    if len(dated) < MIN_DATED or len(dated) < MIN_COVERAGE * len(sequence):
        return UNKNOWN
    pairs = list(zip(dated, dated[1:], strict=False))
    descending = all(a + JITTER >= b for a, b in pairs)
    ascending = all(a <= b + JITTER for a, b in pairs)
    if descending and ascending:
        return UNKNOWN
    if descending:
        return NEWEST_FIRST
    return OLDEST_FIRST if ascending else UNORDERED


def newest_stamp(document: dict[str, Any]) -> str | None:
    """Data da primeira entrada datada: o topo, que é a mais nova quando a ordem é decrescente."""
    first = next((m for m in dated_sequence(document) if m), None)
    return first.isoformat() if first else None
