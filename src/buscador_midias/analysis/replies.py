"""Leitura da resposta em JSON de um modelo de linguagem."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


def json_items(text: str) -> list[Any]:
    """Lista de itens do JSON da resposta de um modelo (vazia se não for interpretável).

    Aceita ``{"items": [...]}`` e também um array puro ``[...]`` na raiz (alguns modelos ignoram o
    objeto pedido), com ou sem bloco de código ou texto em volta.
    """
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        entries = payload.get("items") if isinstance(payload, dict) else payload
        if isinstance(entries, list):
            return entries
    return []


def _json_candidates(text: str) -> Iterator[str]:
    """Trechos que podem ser o JSON da resposta: o texto todo, o objeto e o array internos."""
    yield text.strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            yield text[start : end + 1]
