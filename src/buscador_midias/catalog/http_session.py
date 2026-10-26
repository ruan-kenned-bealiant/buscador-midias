"""Sessões HTTP públicas e conexões ociosas limitadas, isoladas por fonte e processo."""

from __future__ import annotations

import atexit
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.request import Request


class HttpSessions:
    """Compartilha apenas conexões livres; cookies nunca são compartilhados entre fontes."""

    def __init__(self, capacity: int = 32, idle_seconds: float = 30) -> None:
        """Limita memória e sockets sem criar tentativas adicionais de rede."""
        self.capacity, self.idle_seconds = capacity, idle_seconds
        self.lock = threading.RLock()
        self.pid = os.getpid()
        self.connections: OrderedDict[tuple[str, str, str, int], tuple[float, Any]] = OrderedDict()
        self.cookies: OrderedDict[str, CookieJar] = OrderedDict()
        self.cooldowns: dict[str, float] = {}
        self.last_started: dict[str, float] = {}

    def _expire(self) -> None:
        if self.pid != os.getpid():
            self.close()
            self.pid = os.getpid()
        now = time.monotonic()
        for key, (until, connection) in list(self.connections.items()):
            if until <= now:
                self.connections.pop(key)
                connection.close()
        self.cooldowns = {host: until for host, until in self.cooldowns.items() if until > now}
        self.last_started = {host: at for host, at in self.last_started.items() if at > now - 300}

    def acquire(
        self, key: tuple[str, str, str, int], create: Callable[[], Any]
    ) -> tuple[Any, bool]:
        """Retira uma conexão do pool; apenas um leitor pode usá-la por vez."""
        with self.lock:
            self._expire()
            previous = self.connections.pop(key, None)
            if previous is not None:
                return previous[1], True
        return create(), False

    def release(self, key: tuple[str, str, str, int], connection: Any, reusable: bool) -> None:
        """Retém somente respostas totalmente consumidas com keep-alive confirmado."""
        with self.lock:
            if not reusable:
                connection.close()
                return
            old = self.connections.pop(key, None)
            if old:
                old[1].close()
            self.connections[key] = (time.monotonic() + self.idle_seconds, connection)
            while len(self.connections) > self.capacity:
                _, (_, evicted) = self.connections.popitem(last=False)
                evicted.close()

    def _jar(self, namespace: str) -> CookieJar:
        jar = self.cookies.pop(namespace, None) or CookieJar(
            policy=DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrict)
        )
        self.cookies[namespace] = jar
        while len(self.cookies) > 128:
            self.cookies.popitem(last=False)
        jar.clear_expired_cookies()
        # Não acumula indefinidamente cookies de um único site.
        if len(jar) > 128:
            jar.clear()
        return jar

    def cookie_header(self, namespace: str, url: str) -> str | None:
        """Aplica domínio, caminho, validade e Secure usando CookieJar da biblioteca padrão."""
        with self.lock:
            request = Request(url)
            self._jar(namespace).add_cookie_header(request)
            return request.get_header("Cookie")

    def remember(self, namespace: str, url: str, response: Any) -> bool:
        """Aceita somente cookies recebidos publicamente; não importa credenciais externas."""
        if not hasattr(response, "info"):
            return False
        with self.lock:
            jar = self._jar(namespace)
            before = {(c.domain, c.path, c.name): c.value for c in jar}
            jar.extract_cookies(response, Request(url))
            return before != {(c.domain, c.path, c.name): c.value for c in jar}

    def wait(self, host: str, remaining: float) -> None:
        """Mantém ao menos 300 ms entre inícios de acesso ao mesmo host neste processo."""
        with self.lock:
            now = time.monotonic()
            at = max(now, self.last_started.get(host, 0) + 0.3)
            delay = at - now
            if delay >= remaining:
                raise TimeoutError("Orçamento insuficiente para o intervalo por host")
            self.last_started[host] = at
        if delay:
            time.sleep(delay)

    def defer(self, host: str, seconds: int) -> None:
        """Adia o hostname entre leitores deste processo, sem dormir em um worker."""
        with self.lock:
            self._expire()
            self.cooldowns[host] = max(self.cooldowns.get(host, 0), time.monotonic() + seconds)

    def remaining(self, host: str) -> int:
        """Retorna prazo arredondado para cima para não antecipar a próxima chamada."""
        import math

        with self.lock:
            self._expire()
            return max(0, math.ceil(self.cooldowns.get(host, 0) - time.monotonic()))

    def close(self) -> None:
        """Encerra sockets ociosos e elimina estado ao finalizar ou trocar de processo."""
        with self.lock:
            for _, connection in self.connections.values():
                with suppress(OSError):
                    connection.close()
            self.connections.clear()
            self.cookies.clear()
            self.cooldowns.clear()
            self.last_started.clear()


SESSIONS = HttpSessions()
atexit.register(SESSIONS.close)
