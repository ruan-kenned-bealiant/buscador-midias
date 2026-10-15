"""Montagem da pilha de fetchers: HTTP direto, retry, fallback opcional e intervalo por host."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import httpx

from buscador_midias.net.brightdata import DEFAULT_BASE_URL, BrightDataFetcher
from buscador_midias.net.fallback import FallbackFetcher
from buscador_midias.net.fetcher import (
    DEFAULT_MAX_BYTES,
    Fetcher,
    HttpxFetcher,
    RetryingFetcher,
    ThrottledFetcher,
)
from buscador_midias.net.limited import LimitedFetcher

API_KEY_VAR = "BRIGHTDATA_API_KEY"
ZONE_VAR = "BRIGHTDATA_ZONE_UNLOCKER"
BASE_URL_VAR = "BRIGHTDATA_BASE_URL"


class MissingCredentialsError(ValueError):
    """O Bright Data foi pedido, mas faltam credenciais no ambiente."""


@dataclass(frozen=True, slots=True)
class FetchStack:
    """A pilha de fetchers pronta para uso.

    Attributes:
        fetcher: Fetcher completo (intervalo por host → fallback opcional → retry → HTTP).
        fallback: A camada de fallback, quando habilitada; expõe o uso para acompanhar custo.
    """

    fetcher: Fetcher
    fallback: FallbackFetcher | None


def build_fetcher(
    *,
    client: httpx.Client,
    interval: float,
    bright_data: bool,
    env: Mapping[str, str],
    max_fallback_requests: int,
    known_fallback_hosts: Iterable[str] = (),
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FetchStack:
    """Monta a pilha de fetchers.

    A ordem, de fora para dentro, é: intervalo por host → fallback pago (opcional) → retry →
    HTTP direto. O intervalo vale também para as chamadas ao fallback.

    Args:
        client: Cliente httpx compartilhado.
        interval: Segundos mínimos entre requisições ao mesmo host.
        bright_data: Se ``True``, usa o Web Unlocker para sites que bloqueiam.
        env: Variáveis de ambiente (``BRIGHTDATA_API_KEY``, ``BRIGHTDATA_ZONE_UNLOCKER`` e,
            opcionalmente, ``BRIGHTDATA_BASE_URL``).
        max_fallback_requests: Teto de chamadas ao Web Unlocker (controle de custo).
        known_fallback_hosts: Hosts que já bloquearam em rodadas anteriores; vão direto ao
            Web Unlocker.
        max_bytes: Máximo de bytes de corpo aceitos em cada resposta (acima disso vira erro).

    Returns:
        A pilha montada.

    Raises:
        MissingCredentialsError: Se ``bright_data`` for pedido e faltar chave ou zona.
    """
    direct: Fetcher = RetryingFetcher(HttpxFetcher(client, max_bytes=max_bytes))
    if not bright_data:
        return FetchStack(ThrottledFetcher(direct, min_interval=interval), fallback=None)

    api_key, zone = env.get(API_KEY_VAR), env.get(ZONE_VAR)
    if not api_key or not zone:
        raise MissingCredentialsError(f"defina {API_KEY_VAR} e {ZONE_VAR} para usar o Bright Data")

    unlocker = BrightDataFetcher(
        client,
        api_key=api_key,
        zone=zone,
        base_url=env.get(BASE_URL_VAR) or DEFAULT_BASE_URL,
    )
    fallback = FallbackFetcher(
        direct,
        unlocker,
        max_fallback_requests=max_fallback_requests,
        initial_blocked_hosts=known_fallback_hosts,
    )
    return FetchStack(ThrottledFetcher(fallback, min_interval=interval), fallback=fallback)


def build_unlocker(
    client: httpx.Client, env: Mapping[str, str], *, max_requests: int | None
) -> LimitedFetcher:
    """Monta só o Bright Data Web Unlocker, com teto de chamadas (para capturar evidência).

    Args:
        client: Cliente httpx.
        env: Variáveis de ambiente (``BRIGHTDATA_API_KEY``, ``BRIGHTDATA_ZONE_UNLOCKER`` e,
            opcionalmente, ``BRIGHTDATA_BASE_URL``).
        max_requests: Teto de chamadas pagas nesta execução (``None``: sem teto).

    Raises:
        MissingCredentialsError: Se faltar chave ou zona.
    """
    api_key, zone = env.get(API_KEY_VAR), env.get(ZONE_VAR)
    if not api_key or not zone:
        raise MissingCredentialsError(f"defina {API_KEY_VAR} e {ZONE_VAR} para usar o Bright Data")
    unlocker = BrightDataFetcher(
        client,
        api_key=api_key,
        zone=zone,
        base_url=env.get(BASE_URL_VAR) or DEFAULT_BASE_URL,
    )
    return LimitedFetcher(unlocker, max_requests=max_requests)
