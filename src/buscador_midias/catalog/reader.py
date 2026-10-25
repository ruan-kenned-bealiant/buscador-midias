"""Leitor HTTP finito, sem proxy nem consulta a robots, com conexão em IP público."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from urllib.parse import quote, urljoin, urlsplit

from buscador_midias.catalog.compression import MAX_BYTES as MAX_BYTES
from buscador_midias.catalog.compression import unpack_gzip as unpack_gzip
from buscador_midias.catalog.store import CatalogError, clean_url
from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.net.fetcher import USER_AGENT, FetchResult

_DNS = ThreadPoolExecutor(max_workers=32, thread_name_prefix="catalog-dns")


class TestLimitError(Exception):
    """A amostra atingiu um limite próprio, sem reprovar o mecanismo."""

    __test__ = False


def public_ip(host: str, timeout: float) -> str:
    """Resolve uma vez e recusa respostas privadas, inclusive em respostas DNS mistas."""
    future = _DNS.submit(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM)
    try:
        addresses = future.result(timeout=timeout)
    except FutureTimeout as error:
        future.cancel()
        raise OSError("DNS excedeu o tempo de teste") from error
    ips = list(dict.fromkeys(str(item[4][0]) for item in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise OSError("O endereço resolve para uma rede não pública")
    return ips[0]


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, secure: bool, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip, self._secure, self._timeout = ip, secure, timeout

    def connect(self) -> None:
        """Conecta ao IP validado, mantendo Host, SNI e verificação do certificado original."""
        raw = socket.create_connection((self._ip, self.port), self._timeout)
        try:
            self.sock = (
                ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
                if self._secure
                else raw
            )
        except Exception:
            raw.close()
            raise


class TestReader:
    """Rede direta com orçamento configurável, contando cada redirecionamento."""

    __test__ = False

    def __init__(
        self,
        roots: list[str],
        progress: Callable[[dict[str, object]], None],
        *,
        seconds: float = 45,
        requests: int = 24,
        max_bytes: int = MAX_BYTES,
        before_request: Callable[[str], None] | None = None,
        policy_cache: dict[str, tuple[float, RobotsPolicy]] | None = None,
    ) -> None:
        """Define escopo explícito e orçamento por teste."""
        self.deadline = time.monotonic() + seconds
        self.max_requests = requests
        self.max_bytes = max_bytes
        self.requests = 0
        self.before_request = before_request
        self.progress = progress
        self.hosts = [(urlsplit(u).hostname or "").removeprefix("www.") for u in roots]
        # Compatibilidade com scripts históricos: políticas antigas não são aplicadas.
        self.policies: dict[str, RobotsPolicy | None] = {}
        self.last: dict[str, float] = {}
        self.policy_cache = policy_cache if policy_cache is not None else {}
        self.conditional_headers: dict[str, dict[str, str]] = {}

    def _check(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.requests >= self.max_requests:
            raise TestLimitError("Atingido o limite de tempo ou de requisições da amostra.")
        return remaining

    def _scope(self, url: str) -> str:
        url = clean_url(url)
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        if not any(host == root or host.endswith("." + root) for root in self.hosts):
            raise CatalogError("Destino fora dos domínios do site e do mecanismo.")
        return url

    def _allowed(self, url: str) -> tuple[bool, str]:
        """Acessa diretamente, sem consultar ou aplicar regras de robots.txt.

        O hook permanece compatível com leitores e scripts históricos. A validação
        de domínio/IP/TLS continua no transporte, independente deste hook.
        """
        return True, ""

    def get(self, url: str) -> FetchResult:
        """Lê o destino diretamente, validando o escopo de cada redirecionamento."""
        try:
            return self._fetch(url)
        except (CatalogError, OSError, ValueError, http.client.HTTPException) as error:
            if time.monotonic() >= self.deadline:
                raise TestLimitError("Tempo máximo da amostra esgotado.") from error
            return FetchResult(url, None, b"", str(error)[:350])

    def _fetch(self, url: str, *, robots: bool = False) -> FetchResult:
        original = url
        for _ in range(6):
            self._check()
            url = self._scope(url)
            if not robots:
                allowed, reason = self._allowed(url)
                if not allowed:
                    return FetchResult(original, None, b"", reason, final_url=url)
            parts = urlsplit(url)
            host = parts.hostname or ""
            remaining = self._check()
            delay = max(0.0, self.last.get(host, 0) + 0.3 - time.monotonic())
            if delay:
                time.sleep(min(delay, remaining))
            if self.before_request is not None:
                self.before_request(host)
            ip = public_ip(host, min(3, self._check()))
            connection = _PinnedConnection(
                host,
                ip,
                parts.port or (443 if parts.scheme == "https" else 80),
                parts.scheme == "https",
                min(5, self._check()),
            )
            self.requests += 1

            def abort(conn: _PinnedConnection = connection) -> None:
                # Interrompe também cabeçalhos enviados lentamente, antes de read1().
                sock = conn.sock
                if sock is not None:
                    with suppress(OSError):
                        sock.shutdown(socket.SHUT_RDWR)

            timer = threading.Timer(max(0.01, self.deadline - time.monotonic()), abort)
            timer.daemon = True
            timer.start()
            try:
                conditional = {
                    k: v
                    for k, v in self.conditional_headers.get(url, {}).items()
                    if k in ("If-None-Match", "If-Modified-Since")
                    and len(v) <= 1024
                    and "\r" not in v
                    and "\n" not in v
                }
                connection.request(
                    "GET",
                    quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
                    + (
                        "?" + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~") if parts.query else ""
                    ),
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept-Encoding": "identity",
                        **conditional,
                    },
                )
                response = connection.getresponse()
                status = response.status
                mime = response.getheader("Content-Type")
                self.progress(dict(stage="http", message=f"HTTP {status}", url=url, status=status))
                if status in (301, 302, 303, 307, 308):
                    target = response.getheader("Location")
                    if not target:
                        return FetchResult(
                            original, status, b"", "Redirecionamento sem destino", url
                        )
                    url = urljoin(url, target)
                    continue
                chunks: list[bytes] = []
                size = 0
                while True:
                    if time.monotonic() >= self.deadline:
                        raise TestLimitError("Tempo de leitura da amostra esgotado.")
                    chunk = response.read1(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise TestLimitError(
                            f"Resposta acima de {self.max_bytes // (1024 * 1024)} MiB; "
                            "teste parcial."
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                if response.getheader("Content-Encoding", "").lower() == "gzip":
                    body = unpack_gzip(body, self.max_bytes)
                return FetchResult(
                    original,
                    status,
                    body,
                    None,
                    url,
                    mime,
                    etag=response.getheader("ETag"),
                    last_modified=response.getheader("Last-Modified"),
                )
            finally:
                timer.cancel()
                self.last[host] = time.monotonic()
                connection.close()
        return FetchResult(original, None, b"", "Limite de cinco redirecionamentos atingido", url)
