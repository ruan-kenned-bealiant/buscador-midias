"""Cliente do Bright Data Web Unlocker, usado como fallback para sites que bloqueiam o crawler."""

from __future__ import annotations

import threading

import httpx

from buscador_midias.net.fetcher import DEFAULT_MAX_BYTES, FetchResult

DEFAULT_BASE_URL = "https://api.brightdata.com"
DEFAULT_TIMEOUT_SECONDS = 60.0


class BrightDataFetcher:
    """Busca uma URL pelo Web Unlocker (``POST {base_url}/request``, ``format: raw``).

    Segue o mesmo contrato já usado pela captura de evidência do KyX. Cada chamada consome
    créditos, por isso o contador ``requests_made`` existe para o operador acompanhar o custo.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        zone: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """Cria o cliente.

        Args:
            client: Cliente httpx (injetável para testes com ``MockTransport``).
            api_key: Token da API. Nunca é incluído em mensagens de erro.
            zone: Zona do Web Unlocker.
            base_url: Endereço da API, sem barra final obrigatória.
            timeout: Tempo limite de cada chamada, em segundos (o unlocker é lento).
            max_bytes: Máximo de bytes de corpo aceitos; acima disso vira erro.
        """
        self._client = client
        self._api_key = api_key
        self._zone = zone
        self._endpoint = f"{base_url.rstrip('/')}/request"
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._requests_made = 0

    @property
    def requests_made(self) -> int:
        """Quantas chamadas ao unlocker foram feitas (cada uma consome créditos)."""
        return self._requests_made

    def get(self, url: str) -> FetchResult:
        """Pede ao unlocker o conteúdo de ``url``.

        Args:
            url: URL de destino (o site bloqueado).

        Returns:
            O resultado; falhas de transporte voltam em ``error`` com ``status=None``.
        """
        with self._lock:
            self._requests_made += 1
        try:
            response = self._client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}", "Connection": "close"},
                json={"zone": self._zone, "url": url, "format": "raw"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            return FetchResult(
                url=url, status=None, content=b"", error=f"{type(error).__name__}: {error}"
            )

        if not response.is_success:
            return FetchResult(url=url, status=response.status_code, content=b"", error=None)
        body = response.content
        if not body:
            return FetchResult(
                url=url, status=response.status_code, content=b"", error="resposta vazia"
            )
        if len(body) > self._max_bytes:
            return FetchResult(
                url=url,
                status=response.status_code,
                content=b"",
                error=f"corpo acima do limite de {self._max_bytes} bytes",
            )
        return FetchResult(url=url, status=response.status_code, content=body, error=None)
