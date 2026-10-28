"""Fetchers HTTP: cliente com limite de tamanho e decorador de intervalo por host."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

USER_AGENT = "BuscadorMidiasBot/0.1"
DEFAULT_MAX_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Resultado de uma requisição GET; nunca levanta exceção de rede.

    Attributes:
        url: URL requisitada.
        status: Status HTTP, ou ``None`` se a requisição falhou antes de haver resposta.
        content: Corpo da resposta (vazio em falha ou quando excede o limite).
        error: Descrição da falha de transporte ou de limite, se houve.
        final_url: URL final depois dos redirecionamentos (``None`` se não se aplica).
        content_type: Cabeçalho ``Content-Type`` da resposta, se houve.
        redirect_url: Cabeçalho Location quando o transporte retorna um redirecionamento.
        truncated: ``True`` quando o chamador dispensou o resto do corpo (leitura parcial).
    """

    url: str
    status: int | None
    content: bytes
    error: str | None
    final_url: str | None = None
    content_type: str | None = None
    redirect_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    retry_after_seconds: int | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        """``True`` para resposta 2xx sem erro."""
        return self.error is None and self.status is not None and 200 <= self.status < 300


class Fetcher(Protocol):
    """Porta para requisições GET, trocável por um fake nos testes."""

    def get(self, url: str) -> FetchResult:
        """Busca a URL e devolve o resultado."""
        ...


class HttpxFetcher:
    """Fetcher sobre ``httpx.Client`` que limita o tamanho do corpo lido."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        user_agent: str = USER_AGENT,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """Cria o fetcher.

        Args:
            client: Cliente httpx (injetado para permitir ``MockTransport`` nos testes).
            user_agent: Valor do cabeçalho ``User-Agent``.
            max_bytes: Máximo de bytes de corpo aceitos; acima disso vira erro.
        """
        self._client = client
        self._headers = {"User-Agent": user_agent}
        self._max_bytes = max_bytes

    def get(self, url: str) -> FetchResult:
        """Faz o GET, lendo o corpo em streaming até o limite.

        Args:
            url: URL a buscar.

        Returns:
            O resultado; falhas de transporte voltam em ``error`` com ``status=None``.
        """
        try:
            return self._get_once(url)
        except UnicodeEncodeError as error:
            failure = str(error)
            removed = False
            for cookie in list(self._client.cookies.jar):
                if any(
                    value is not None and not value.isascii()
                    for value in (cookie.name, cookie.value, cookie.domain, cookie.path)
                ):
                    try:
                        self._client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
                        removed = True
                    except KeyError:
                        pass  # Outra thread já removeu esse cookie inválido.
            if removed:
                try:
                    return self._get_once(url)
                except UnicodeEncodeError as retry_error:
                    failure = str(retry_error)
            return FetchResult(url, None, b"", "UnicodeEncodeError: " + failure)

    def _get_once(self, url: str) -> FetchResult:
        """Executa a leitura; o chamador trata cookies não representáveis no cabeçalho."""
        try:
            with self._client.stream("GET", url, headers=self._headers) as response:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self._max_bytes:
                        return FetchResult(
                            url=url,
                            status=response.status_code,
                            content=b"",
                            error=f"corpo acima do limite de {self._max_bytes} bytes",
                        )
                    chunks.append(chunk)
                return FetchResult(
                    url=url,
                    status=response.status_code,
                    content=b"".join(chunks),
                    error=None,
                    final_url=str(response.url),
                    content_type=response.headers.get("content-type"),
                )
        except httpx.HTTPError as error:
            return FetchResult(
                url=url, status=None, content=b"", error=f"{type(error).__name__}: {error}"
            )


_RETRYABLE_STATUSES = frozenset({502, 503, 504})
_RETRYABLE_ERRORS = frozenset(
    {"ReadTimeout", "ConnectTimeout", "PoolTimeout", "RemoteProtocolError", "ReadError"}
)


class RetryingFetcher:
    """Decorador que repete falhas transitórias com espera exponencial.

    Repete timeouts, quedas de conexão e HTTP 502/503/504. Não repete o que é permanente ou não
    muda ao tentar de novo: 4xx, HTTP 500, falha de DNS, erro de certificado, corpo grande demais.
    """

    def __init__(
        self,
        inner: Fetcher,
        *,
        attempts: int = 2,
        backoff: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Cria o decorador.

        Args:
            inner: Fetcher a ser protegido.
            attempts: Número total de tentativas (1 = sem repetição).
            backoff: Espera antes da 2ª tentativa; dobra a cada nova tentativa.
            sleep: Função de espera (injetável para testes).
        """
        self._inner = inner
        self._attempts = attempts
        self._backoff = backoff
        self._sleep = sleep

    def get(self, url: str) -> FetchResult:
        """Busca a URL, repetindo enquanto a falha for transitória e houver tentativas."""
        result = self._inner.get(url)
        delay = self._backoff
        for _ in range(self._attempts - 1):
            if not _is_transient(result):
                break
            self._sleep(delay)
            delay *= 2
            result = self._inner.get(url)
        return result


def _is_transient(result: FetchResult) -> bool:
    """Indica se vale a pena repetir a requisição."""
    if result.status is None:
        error_type = (result.error or "").split(":", 1)[0]
        return error_type in _RETRYABLE_ERRORS
    return result.error is None and result.status in _RETRYABLE_STATUSES


class ThrottledFetcher:
    """Decorador que garante um intervalo mínimo entre requisições ao mesmo host.

    Seguro para uso por várias threads: cada host tem seu próprio trava, então hosts diferentes
    seguem em paralelo e o mesmo host é atendido uma requisição por vez.
    """

    def __init__(
        self,
        inner: Fetcher,
        *,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Cria o decorador.

        Args:
            inner: Fetcher real a ser protegido.
            min_interval: Segundos mínimos entre o fim de uma requisição e o início da próxima.
            clock: Relógio monotônico (injetável para testes).
            sleep: Função de espera (injetável para testes).
        """
        self._inner = inner
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._registry_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last_finished: dict[str, float] = {}

    def get(self, url: str) -> FetchResult:
        """Espera o intervalo do host, se preciso, e delega ao fetcher interno."""
        host = host_key(url)
        with self._lock_for(host):
            last = self._last_finished.get(host)
            if last is not None:
                wait = last + self._min_interval - self._clock()
                if wait > 0:
                    self._sleep(wait)
            try:
                return self._inner.get(url)
            finally:
                self._last_finished[host] = self._clock()

    def _lock_for(self, host: str) -> threading.Lock:
        """Devolve (criando, se preciso) a trava do host."""
        with self._registry_lock:
            return self._host_locks.setdefault(host, threading.Lock())


def host_key(url: str) -> str:
    """Host em minúsculas sem ``www.``, para tratar com e sem ``www`` como o mesmo servidor."""
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")
