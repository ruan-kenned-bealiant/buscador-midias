"""Classificação de falhas HTTP e espera informada pelo servidor."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def retry_after(value: str | None, now: datetime | None = None) -> int | None:
    """Interpreta segundos ou data HTTP, sem aceitar negativos e valores fora do intervalo."""
    if not value or len(value) > 128:
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            seconds = int(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = max(0, math.ceil((date - (now or datetime.now(UTC))).total_seconds()))
        return seconds if 0 <= seconds <= 2**31 - 1 else None
    except (ValueError, TypeError, OverflowError):
        return None


def retryable(status: int | None, error: str | None) -> bool:
    """Não repete endereço ausente, acesso negado, certificado inválido ou ciclo."""
    if status in (400, 401, 403, 404, 405, 410, 451, 501):
        return False
    return not any(
        token in (error or "")
        for token in (
            "redirect_cycle:",
            "redirect_limit:",
            "certificate verify failed",
            "CERTIFICATE_VERIFY_FAILED",
            "fora dos domínios",
            "não pública",
        )
    )
