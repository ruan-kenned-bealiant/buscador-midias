"""Cliente do Bright Data Web Unlocker, usado como fallback para sites que bloqueiam o crawler."""

from __future__ import annotations

import base64
import threading
import time

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
        structured: bool = False,
    ) -> None:
        """Cria o cliente.

        Args:
            client: Cliente httpx (injetável para testes com ``MockTransport``).
            api_key: Token da API. Nunca é incluído em mensagens de erro.
            zone: Zona do Web Unlocker.
            base_url: Endereço da API, sem barra final obrigatória.
            timeout: Tempo limite de cada chamada, em segundos (o unlocker é lento).
            max_bytes: Máximo de bytes de corpo aceitos; acima disso vira erro.
            structured: Usa envelope JSON com status e cabeçalhos do destino.
        """
        self._client = client
        self._api_key = api_key
        self._zone = zone
        self._endpoint = f"{base_url.rstrip('/')}/request"
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._structured = structured
        self._lock = threading.Lock()
        self._requests_made = 0

    @property
    def requests_made(self) -> int:
        """Quantas chamadas ao unlocker foram feitas (cada uma consome créditos)."""
        return self._requests_made

    def get(self, url: str, *, timeout: float | None = None) -> FetchResult:
        """Pede ao unlocker o conteúdo de ``url``.

        Args:
            url: URL de destino (o site bloqueado).
            timeout: Orçamento da chamada estruturada; padrão do cliente quando omitido.

        Returns:
            O resultado; falhas de transporte voltam em ``error`` com ``status=None``.
        """
        if self._structured:
            return self._get_structured(url, timeout or self._timeout)
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

    def _get_structured(self, url: str, timeout: float) -> FetchResult:
        """Preserva status e metadados do destino, sem confundi-los com o HTTP da API."""
        with self._lock:
            self._requests_made += 1
        deadline = time.monotonic() + timeout
        try:
            with self._client.stream(
                "POST",
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"zone": self._zone, "url": url, "format": "json"},
                timeout=timeout,
            ) as response:
                if not response.is_success:
                    return FetchResult(
                        url, None, b"", f"bright_data_api_error: HTTP {response.status_code}"
                    )
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self._max_bytes * 6 + 65536 or time.monotonic() >= deadline:
                        return FetchResult(url, None, b"", "bright_data_limit: limite de leitura")
                    chunks.append(chunk)
                import json

                payload = json.loads(b"".join(chunks))
            if not isinstance(payload, dict) or type(payload.get("status_code")) is not int:
                return FetchResult(
                    url, None, b"", "bright_data_api_error: resposta sem status do destino"
                )
            body = payload.get("body", "")
            if not isinstance(body, str):
                raise ValueError("Corpo inválido")
            content = (
                base64.b64decode(body, validate=True)
                if payload.get("body_encoding", payload.get("encoding")) == "base64"
                else body.encode("utf-8")
            )
            if len(content) > self._max_bytes:
                return FetchResult(url, None, b"", "bright_data_limit: resposta acima do limite")
            headers = {str(k).lower(): str(v) for k, v in (payload.get("headers") or {}).items()}
            final = payload.get("final_url") or payload.get("url") or url
            return FetchResult(
                url,
                payload["status_code"],
                content,
                None,
                final if isinstance(final, str) else url,
                headers.get("content-type"),
                headers.get("location"),
            )
        except httpx.TimeoutException:
            return FetchResult(url, None, b"", "bright_data_limit: tempo de acesso esgotado")
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return FetchResult(
                url, None, b"", "bright_data_api_error: falha de comunicação ou resposta inválida"
            )
