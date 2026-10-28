"""Transporte curl opcional, com os mesmos limites e proteções do leitor direto."""

from __future__ import annotations

import threading
import time
import weakref
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

from buscador_midias.catalog.http_policy import retry_after
from buscador_midias.catalog.http_transport import validate_transport
from buscador_midias.catalog.reader import (
    EarlyStop,
    TestLimitError,
    TestReader,
    early_stop_due,
    public_ip,
)
from buscador_midias.net.fetcher import USER_AGENT, FetchResult

_IPS: dict[str, tuple[float, str]] = {}
_IP_LOCK = threading.Lock()


def _public_address(host: str, timeout: float) -> str:
    """Reutiliza por um minuto IP já validado e sempre fixa a conexão nesse IP público."""
    with _IP_LOCK:
        cached = _IPS.get(host)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    address = public_ip(host, timeout)
    with _IP_LOCK:
        if len(_IPS) >= 1024:
            _IPS.clear()
        _IPS[host] = (time.monotonic() + 60, address)
    return address


class CurlReader(TestReader):
    """Usa curl sem proxy, IP público fixado, TLS verificado e um perfil explícito."""

    def __init__(
        self,
        roots: list[str],
        progress: Callable[[dict[str, object]], None],
        *,
        transport: str = "curl_chrome_h2",
        **kwargs: Any,
    ) -> None:
        """Importa a dependência somente quando um link seleciona curl."""
        from curl_cffi import CurlHttpVersion, requests

        super().__init__(roots, progress, **kwargs)
        self.transport = validate_transport(transport)
        if self.transport == "direct":
            raise ValueError("Use TestReader para o transporte direto.")
        self.session: Any = requests.Session(
            trust_env=False,
            verify=True,
            impersonate=None
            if "_bot_" in transport
            else "firefox"
            if "firefox" in transport
            else "chrome",
            headers={"User-Agent": USER_AGENT} if "_bot_" in transport else None,
            http_version=CurlHttpVersion.V1_1
            if transport.endswith("_h1")
            else CurlHttpVersion.V2_PRIOR_KNOWLEDGE,
        )
        self.transport_events: list[dict[str, Any]] = []
        self._finalizer = weakref.finalize(self, self.session.close)

    def close(self) -> None:
        """Fecha a sessão, inclusive quando o chamador interrompe a amostra."""
        self._finalizer()

    def _fetch(
        self, url: str, *, robots: bool = False, early_stop: EarlyStop | None = None
    ) -> FetchResult:
        from curl_cffi import CurlHttpVersion, CurlOpt

        original = url
        visited: set[str] = set()
        for _ in range(6):
            url = self._scope(url)
            if url in visited:
                return FetchResult(original, None, b"", "redirect_cycle: ciclo", url)
            visited.add(url)
            parts = urlsplit(url)
            host = parts.hostname or ""
            if self.before_request:
                self.before_request(host)
            wait = self.sessions.remaining(host)
            if wait:
                self.retry_after_seconds, self.retry_after_host = wait, host
                self.cooldown_deferred = True
                return FetchResult(
                    original, None, b"", "host_cooldown", url, retry_after_seconds=wait
                )
            self.sessions.wait(host, self._check())
            remaining = self._check()
            size = 0
            try:
                ip = _public_address(host, min(3, remaining))
                port = parts.port or (443 if parts.scheme == "https" else 80)
                address = f"[{ip}]" if ":" in ip else ip
                self.session.curl_options[CurlOpt.RESOLVE] = [f"{host}:{port}:{address}"]
                chunks: list[bytes] = []
                size = 0
                step, stopped = 0, False

                def receive(chunk: bytes, target: list[bytes] = chunks) -> int:
                    nonlocal size, step, stopped
                    if stopped:
                        # O curl pode seguir entregando depois do primeiro 0: nada mais entra.
                        return 0
                    size += len(chunk)
                    if size > self.max_bytes or time.monotonic() >= self.deadline:
                        return 0
                    target.append(chunk)
                    if early_stop:
                        due, step = early_stop_due(size, step)
                        if due and early_stop(b"".join(target)):
                            stopped = True
                            return 0
                    return len(chunk)

                headers = {
                    key: value
                    for key, value in self.conditional_headers.get(url, {}).items()
                    if key in ("If-None-Match", "If-Modified-Since")
                    and len(value) <= 1024
                    and "\r" not in value
                    and "\n" not in value
                }
                request_timeout = min(8, max(0.05, self._check()))
                self.requests += 1
                try:
                    response = self.session.get(
                        url,
                        headers=headers or None,
                        allow_redirects=False,
                        timeout=request_timeout,
                        content_callback=receive,
                        # HTTP sem TLS não anuncia h2c a servidores RSS legados.
                        http_version=(
                            CurlHttpVersion.V1_1
                            if parts.scheme == "http" or self.transport.endswith("_h1")
                            else CurlHttpVersion.V2_PRIOR_KNOWLEDGE
                        ),
                    )
                except Exception:
                    if not stopped:
                        raise
                    # Transferência encerrada de propósito: o erro de escrita do curl é esperado.
                    self.last_http_status = 200
                    return FetchResult(original, 200, b"".join(chunks), None, url, truncated=True)
                if stopped:
                    self.last_http_status = 200
                    return FetchResult(original, 200, b"".join(chunks), None, url, truncated=True)
                if size > self.max_bytes:
                    raise TestLimitError("Resposta curl acima do limite da amostra.")
                if time.monotonic() >= self.deadline:
                    raise TestLimitError("Orçamento curl esgotado.")
                status = int(response.status_code)
                self.last_http_status = status
                after = (
                    retry_after(response.headers.get("Retry-After"))
                    if status in (429, 503)
                    else None
                )
                if status == 429 and after is None:
                    after = 60
                if after:
                    self.sessions.defer(host, after)
                    self.retry_after_seconds, self.retry_after_host = after, host
                protocol = "h2" if int(response.http_version) == 3 else "http/1.1"
                self.transport_events.append(
                    dict(url=url, transport=self.transport, status=status, protocol=protocol)
                )
                self.progress(
                    dict(
                        stage="http", message=f"HTTP {status} · {protocol}", url=url, status=status
                    )
                )
                if status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        return FetchResult(original, status, b"", "redirect_without_location", url)
                    url = urljoin(url, location)
                    continue
                return FetchResult(
                    original,
                    status,
                    b"".join(chunks),
                    None,
                    url,
                    response.headers.get("Content-Type"),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    retry_after_seconds=after,
                )
            except TestLimitError:
                raise
            except Exception as error:
                if size > self.max_bytes:
                    raise TestLimitError("Resposta curl acima do limite da amostra.") from error
                if time.monotonic() >= self.deadline:
                    raise TestLimitError("Orçamento curl esgotado.") from error
                return FetchResult(
                    original, None, b"", f"{type(error).__name__}: {error}"[:300], url
                )
        return FetchResult(original, None, b"", "redirect_limit", url)
