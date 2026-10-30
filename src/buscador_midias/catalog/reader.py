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
from buscador_midias.catalog.http_policy import retry_after
from buscador_midias.catalog.http_session import SESSIONS, HttpSessions
from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.catalog.urls import clean_url
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


# Prefixos (em bytes) em que o chamador é consultado sobre dispensar o resto do corpo. Feeds de
# itens enormes só mostram as primeiras datas em prefixos maiores, por isso a pergunta se repete.
EARLY_STOP_STEPS = (32 * 1024, 128 * 1024, 512 * 1024, 2 * 1024 * 1024)
EARLY_STOP_BYTES = EARLY_STOP_STEPS[0]
EarlyStop = Callable[[bytes], bool]


def early_stop_due(size: int, step: int) -> tuple[bool, int]:
    """Indica se ``size`` alcançou o próximo degrau de pergunta e qual é o degrau seguinte."""
    if step >= len(EARLY_STOP_STEPS) or size < EARLY_STOP_STEPS[step]:
        return False, step
    while step < len(EARLY_STOP_STEPS) and size >= EARLY_STOP_STEPS[step]:
        step += 1
    return True, step


class TestReader:
    """Rede direta com orçamento configurável, contando cada redirecionamento."""

    __test__ = False
    # Aceita ``early_stop`` em ``get``; subclasses que sobrescrevem ``get`` desligam.
    supports_early_stop = True

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
        sessions: HttpSessions | None = None,
        host_gap: float = 0.3,
    ) -> None:
        """Define escopo explícito e orçamento por teste; ``host_gap`` é o respiro entre acessos."""
        self.deadline = time.monotonic() + seconds
        self.max_requests = requests
        self.max_bytes = max_bytes
        self.requests = 0
        self.before_request = before_request
        self.host_gap = host_gap
        self.progress = progress
        self.hosts = [(urlsplit(u).hostname or "").removeprefix("www.") for u in roots]
        # Compatibilidade com scripts históricos: políticas antigas não são aplicadas.
        self.policies: dict[str, RobotsPolicy | None] = {}
        self.last: dict[str, float] = {}
        self.policy_cache = policy_cache if policy_cache is not None else {}
        self.sessions = sessions if sessions is not None else SESSIONS
        self.namespace = (urlsplit(roots[0]).hostname or "").lower().removeprefix("www.")
        self.connections_reused = 0
        self.cookies_sent = 0
        self.session_updated = False
        self.retry_after_seconds: int | None = None
        self.retry_after_host: str | None = None
        self.last_http_status: int | None = None
        self.cooldown_deferred = False
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

    def get(self, url: str, *, early_stop: EarlyStop | None = None) -> FetchResult:
        """Lê o destino diretamente, validando o escopo de cada redirecionamento.

        ``early_stop`` recebe o prefixo já lido a cada degrau de ``EARLY_STOP_STEPS`` alcançado;
        se devolver ``True`` a leitura termina ali e o resultado vem com ``truncated=True``.
        """
        self.session_updated = False
        self.retry_after_seconds = None
        self.retry_after_host = None
        self.last_http_status = None
        self.cooldown_deferred = False
        try:
            return self._fetch(url, early_stop=early_stop) if early_stop else self._fetch(url)
        except (CatalogError, OSError, ValueError, http.client.HTTPException) as error:
            if time.monotonic() >= self.deadline:
                raise TestLimitError("Tempo máximo da amostra esgotado.") from error
            return FetchResult(url, None, b"", str(error)[:350])

    def _fetch(
        self, url: str, *, robots: bool = False, early_stop: EarlyStop | None = None
    ) -> FetchResult:
        original = url
        visited: set[str] = set()
        for _ in range(6):
            self._check()
            url = self._scope(url)
            if url in visited:
                return FetchResult(
                    original, None, b"", "redirect_cycle: ciclo de redirecionamento", url
                )
            visited.add(url)
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
            wait = self.sessions.remaining(host)
            if wait:
                self.retry_after_seconds, self.retry_after_host = wait, host
                self.cooldown_deferred = True
                return FetchResult(
                    original,
                    None,
                    b"",
                    "host_cooldown: aguardando o servidor",
                    url,
                    retry_after_seconds=wait,
                )
            self.sessions.wait(host, self._check(), self.host_gap)
            key = (
                self.namespace,
                parts.scheme,
                host,
                parts.port or (443 if parts.scheme == "https" else 80),
            )

            def create(
                host: str = host, port: int = key[3], secure: bool = parts.scheme == "https"
            ) -> _PinnedConnection:
                ip = public_ip(host, min(3, self._check()))
                return _PinnedConnection(host, ip, port, secure, min(5, self._check()))

            connection, reused = self.sessions.acquire(key, create)
            self.connections_reused += int(reused)
            if reused:
                try:
                    connection.timeout = connection._timeout = min(5, self._check())
                    if connection.sock is None:
                        raise OSError("Conexão persistente encerrada")
                    connection.sock.settimeout(connection.timeout)
                except Exception:
                    self.sessions.release(key, connection, False)
                    raise
            reusable = False
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
                cookie = self.sessions.cookie_header(self.namespace, url)
                self.cookies_sent += int(bool(cookie))
                connection.request(
                    "GET",
                    quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
                    + (
                        "?" + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~") if parts.query else ""
                    ),
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept-Encoding": "identity",
                        **({"Cookie": cookie} if cookie else {}),
                        **conditional,
                    },
                )
                response = connection.getresponse()
                status = response.status
                self.last_http_status = status
                self.session_updated = self.sessions.remember(self.namespace, url, response)
                after = (
                    retry_after(response.getheader("Retry-After")) if status in (429, 503) else None
                )
                if status == 429 and after is None:
                    after = 60
                if after:
                    self.retry_after_seconds, self.retry_after_host = after, host
                    self.sessions.defer(host, after)
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
                step, stopped = 0, False
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
                    if early_stop and status == 200:
                        due, step = early_stop_due(size, step)
                        if due and early_stop(b"".join(chunks)):
                            stopped = True
                            break
                body = b"".join(chunks)
                if not stopped and response.getheader("Content-Encoding", "").lower() == "gzip":
                    body = unpack_gzip(body, self.max_bytes)
                # Resposta interrompida deixa o resto no soquete: a conexão não pode ser reusada.
                reusable = (
                    not stopped
                    and not getattr(response, "will_close", True)
                    and 200 <= status < 400
                    and time.monotonic() < self.deadline
                )
                return FetchResult(
                    original,
                    status,
                    body,
                    None,
                    url,
                    mime,
                    etag=response.getheader("ETag"),
                    last_modified=response.getheader("Last-Modified"),
                    retry_after_seconds=after,
                    truncated=stopped,
                )
            finally:
                timer.cancel()
                self.last[host] = time.monotonic()
                self.sessions.release(key, connection, reusable)
        return FetchResult(
            original, None, b"", "redirect_limit: cinco redirecionamentos atingidos", url
        )
