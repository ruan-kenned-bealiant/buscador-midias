"""Classifica o motivo de uma falha de notícia: um código curto e uma classe.

A classe decide o que acontece depois da falha, no lugar do regex ``PERMANENT_FAILURE`` que
vivia em SQL:

- ``transient``: pode passar sozinha (tempo esgotado, 5xx, XML ilegível); nova tentativa automática;
- ``blocked``: o site recusa o acesso (401, 402, 403); só o acesso pago ou uma pessoa resolve;
- ``permanent``: repetir não muda nada (404, 410, ciclo de redirecionamento, fora do domínio);
- ``bug``: exceção interna nossa; aparece para investigação e não é repetida sozinha.
"""

from __future__ import annotations

import re
from typing import NamedTuple

DETAIL_LIMIT = 500
AUTO_RETRY_CLASSES = ("transient",)

_HTTP = re.compile(r"HTTP\s+(\d{3})")
_BLOCKED = {401, 402, 403}
_PERMANENT = {404, 410}

# (padrão, código, classe), do mais específico ao mais geral.
_TEXT_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), code, klass)
    for pattern, code, klass in (
        (r"redirect_cycle", "redirect_cycle", "permanent"),
        (r"fora dos dom", "outside_domain", "permanent"),
        (r"robots_unavailable", "robots_unavailable", "permanent"),
        (r"bright_data_api_error", "bright_data_error", "permanent"),
        (r"Falha interna", "internal", "bug"),
        (r"timed? ?out|tempo esgotado|timeout", "timeout", "transient"),
        (r"connection|conex", "connection", "transient"),
        (r"XML inv|incompat", "invalid_xml", "transient"),
        (r"an[aá]lise vazia|resposta.*vazia", "empty_analysis", "transient"),
    )
)


class Failure(NamedTuple):
    """Resultado da classificação."""

    code: str
    klass: str
    detail: str


def classify(reason: str, *, retryable: bool = True) -> Failure:
    """Código e classe; ``retryable=False`` torna permanente o que seria transitório."""
    detail = reason[:DETAIL_LIMIT]
    code, klass = _identify(reason)
    if klass == "transient" and not retryable:
        klass = "permanent"
    return Failure(code, klass, detail)


def _identify(reason: str) -> tuple[str, str]:
    http = _HTTP.search(reason)
    if http:
        status = int(http.group(1))
        if status in _BLOCKED:
            return f"http_{status}", "blocked"
        if status in _PERMANENT:
            return f"http_{status}", "permanent"
        return f"http_{status}", "transient"
    for pattern, code, klass in _TEXT_RULES:
        if pattern.search(reason):
            return code, klass
    return "other", "transient"
