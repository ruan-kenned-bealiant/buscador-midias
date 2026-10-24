"""Fallback pago integrado ao leitor do gerenciador, com os mesmos controles de acesso."""

from __future__ import annotations

import http.client
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from buscador_midias.catalog.reader import TestLimitError, TestReader, public_ip
from buscador_midias.net.brightdata import DEFAULT_BASE_URL, BrightDataFetcher
from buscador_midias.net.fetcher import FetchResult


def bright_data_available() -> bool:
    """Só informa presença de credenciais; nunca retorna chave ou zona ao navegador."""
    return bool(os.environ.get("BRIGHTDATA_API_KEY") and os.environ.get("BRIGHTDATA_ZONE_UNLOCKER"))


def should_fallback(result: FetchResult) -> bool:
    """Não chama serviço pago para XML vazio, áudio, 404, erro de escopo ou robots proibitivo."""
    error = (result.error or "").lower()
    if any(
        word in error
        for word in ("robots_", "não pública", "nao publica", "fora dos domínios", "url pública")
    ):
        return False
    if result.status in (403, 429):
        return True
    if result.status == 401:
        return False  # Autenticação real não pode ser suprida por um proxy.
    body = result.content[:65536].lower()
    if result.ok and any(
        marker in body
        for marker in (
            b"cf-chl-",
            b"challenge-platform",
            b"verify you are human",
            b"checking your browser",
            b"captcha-delivery.com",
        )
    ):
        return True
    return (
        result.status is None
        and bool(result.error)
        and any(
            word in error
            for word in (
                "timed out",
                "timeout",
                "connection",
                "ssl",
                "rede",
                "dns",
                "temporarily",
                "resolution",
                "temporary failure",
                "name or service not known",
                "reset",
                "remote end",
            )
        )
    )


class BrightTestReader(TestReader):
    """Tenta HTTP direto antes de cada fallback, inclusive ao recuperar robots.txt."""

    __test__ = False

    def __init__(
        self,
        roots: list[str],
        progress: Callable[[dict[str, object]], None],
        *,
        max_bright_requests: int = 8,
        client_factory: Callable[..., Any] = httpx.Client,
        **kwargs: Any,
    ) -> None:
        """Contabiliza chamadas e mantém o teto total de tempo, bytes e requisições."""
        super().__init__(roots, progress, **kwargs)
        self.max_bright_requests = max_bright_requests
        self.client_factory = client_factory
        self.bright_events: list[dict[str, Any]] = []
        self.bright_requests = 0
        self.bright_provider_error = False
        self.bright_limit_reached = False
        self._bright_depth = 0

    def _fetch(self, url: str, *, robots: bool = False) -> FetchResult:
        try:
            direct = super()._fetch(url, robots=robots)
        except (OSError, http.client.HTTPException) as error:
            direct = FetchResult(url, None, b"", str(error)[:200], url)
        if not should_fallback(direct) or self.bright_provider_error:
            return direct
        if self.bright_requests >= self.max_bright_requests:
            self.bright_limit_reached = True
            return direct
        if not bright_data_available():
            self.bright_provider_error = True
            self.bright_events.append(dict(url=url, error="Credenciais Bright Data indisponíveis"))
            return direct
        remaining = self._check()
        # Valida novamente o destino antes de o enviar ao provedor. Política desconhecida
        # ou proibitiva continua impedindo a leitura do conteúdo, mesmo com fallback.
        url = self._scope(direct.final_url or url)
        public_ip(urlsplit(url).hostname or "", min(3, remaining))
        if not robots:
            allowed, _ = self._allowed(url)
            if not allowed:
                return direct
        remaining = self._check()
        self.requests += 1
        self.bright_requests += 1
        self.progress(
            dict(
                stage="bright_data",
                message="Tentando Bright Data após falha no acesso direto",
                url=url,
            )
        )
        with self.client_factory(trust_env=False, follow_redirects=False) as client:
            provider = BrightDataFetcher(
                client,
                api_key=os.environ["BRIGHTDATA_API_KEY"],
                zone=os.environ["BRIGHTDATA_ZONE_UNLOCKER"],
                base_url=os.environ.get("BRIGHTDATA_BASE_URL") or DEFAULT_BASE_URL,
                timeout=remaining,
                max_bytes=self.max_bytes,
                structured=True,
            )
            response = provider.get(url, timeout=remaining)
        self.bright_events.append(
            dict(url=url, direct_status=direct.status, status=response.status, error=response.error)
        )
        self.bright_provider_error = bool(
            response.error and response.error.startswith("bright_data_api_error")
        )
        self.bright_limit_reached = self.bright_limit_reached or bool(
            response.error and response.error.startswith("bright_data_limit")
        )
        if time.monotonic() >= self.deadline:
            raise TestLimitError("Tempo máximo do teste com Bright Data esgotado.")
        if response.final_url and response.final_url != url:
            final = self._scope(response.final_url)
            public_ip(urlsplit(final).hostname or "", min(3, self._check()))
            if not robots and not self._allowed(final)[0]:
                return FetchResult(
                    url, None, b"", "Política impede o destino final do fallback", final
                )
        if response.status in (301, 302, 303, 307, 308) and response.redirect_url:
            if self._bright_depth >= 5:
                return FetchResult(url, None, b"", "Limite de redirecionamentos no fallback")
            target = self._scope(urljoin(url, response.redirect_url))
            self._bright_depth += 1
            try:
                return self._fetch(target, robots=robots)
            finally:
                self._bright_depth -= 1
        return response
