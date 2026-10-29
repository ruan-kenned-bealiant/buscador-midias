"""Conversão de linhas do banco em JSON: datas ISO e decimais exatos (custos)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def jsonable(value: Any) -> Any:
    """Datas em ISO 8601 e decimais como texto, recursivamente."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def clean(value: Any) -> Any:
    """Tira o caractere NUL de todo texto: o Postgres o recusa em ``jsonb`` e em ``text``.

    Páginas e feeds mal formados trazem NUL de vez em quando; isso nunca deve virar falha.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {clean(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value
