"""Perfis explícitos de transporte, independentes do mecanismo de parsing e seleção."""

from collections.abc import Callable
from typing import Any

TRANSPORTS = (
    "direct",
    "curl_chrome_h2",
    "curl_chrome_h1",
    "curl_firefox_h2",
    "curl_bot_h2",
    "curl_bot_h1",
)


def validate_transport(value: object) -> str:
    """Não permite opções TLS arbitrárias ou nomes de perfis desconhecidos."""
    if not isinstance(value, str) or value not in TRANSPORTS:
        raise ValueError("Transporte HTTP inválido.")
    return value


def make_reader(
    roots: list[str],
    progress: Callable[[dict[str, object]], None],
    *,
    transport: str = "direct",
    **kwargs: Any,
) -> Any:
    """Mantém o leitor atual como padrão; importa curl somente para links configurados."""
    validate_transport(transport)
    if transport == "direct":
        from buscador_midias.catalog.reader import TestReader

        return TestReader(roots, progress, **kwargs)
    from buscador_midias.catalog.curl_reader import CurlReader

    return CurlReader(roots, progress, transport=transport, **kwargs)
