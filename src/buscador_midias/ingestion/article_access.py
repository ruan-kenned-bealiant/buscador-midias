"""Download da página da notícia: um 403 com o transporte do link não é o fim da conversa.

O cadastro grava um transporte por link, descoberto com o feed ou o sitemap. A página da notícia
costuma ficar atrás de uma proteção mais dura que o feed (impressão digital TLS do cliente), e um
site pode abrir o feed ao cliente comum e recusar as notícias. Em testes com 50 notícias que o
motor marcou como bloqueadas (403), 39 abriram com o curl imitando Chrome e passaram na extração.
Aqui, um 403 tenta os outros transportes, um de cada vez, e o que funcionar vale para as próximas
notícias do mesmo site.

Só o 403 dispara a busca: 401 é paywall (nenhum transporte resolve), 429 e 5xx têm repetição
própria e 404 é página inexistente.

Um site que recusa tudo várias vezes seguidas está nos bloqueando (limite de taxa ou IP marcado), e
insistir só piora: depois de ``REFUSALS_TO_REST`` recusas seguidas ele descansa ``REST_SECONDS`` e
as notícias dele falham sem nenhum pedido. Elas voltam pela repetição automática, horas depois.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from buscador_midias.net.fetcher import FetchResult

#: Em ordem: o perfil de Chrome em HTTP/2 é o que mais abriu; Firefox varia a impressão digital.
FALLBACK_TRANSPORTS = ("curl_chrome_h2", "curl_chrome_h1", "curl_firefox_h2")
REFUSED = (403,)
#: Respiro entre dois pedidos ao mesmo site nas notícias (o dos feeds é 0,3 s): sites com WAF
#: respondem 403 a rajadas, e vários dos 403 "bloqueados" voltaram a abrir horas depois.
ARTICLE_HOST_GAP = 1.5
REMEMBERED = 512
#: Recusas totais seguidas de um site até ele descansar, e por quanto tempo (segundos).
REFUSALS_TO_REST = 3
REST_SECONDS = 1200.0

OpenReader = Callable[[str], AbstractContextManager[Any]]


@dataclass(frozen=True)
class Fetched:
    """A resposta final e o leitor que a produziu (já fechado: só se leem seus contadores).

    Attributes:
        response: A resposta; se nenhum transporte abriu, a recusa original.
        reader: O leitor da resposta devolvida.
        transport: O transporte dessa resposta.
        attempts: Os transportes tentados, na ordem.
        fallback: Se a resposta veio de um transporte diferente do primeiro tentado.
    """

    response: Any
    reader: Any
    transport: str
    attempts: tuple[str, ...]
    fallback: bool = False

    def metrics(self) -> dict[str, Any]:
        """O que registrar no custo da etapa; vazio quando não houve troca de transporte.

        O custo só guarda números, então a troca também vai como contadores
        (``transport_extra_requests``, ``transport_fallbacks``); os nomes seguem no diagnóstico.
        """
        if not self.attempts:
            return dict(transport_skipped=1)
        if len(self.attempts) < 2:
            return {}
        found: dict[str, Any] = dict(
            transport_attempts=list(self.attempts),
            transport_extra_requests=len(self.attempts) - 1,
        )
        if self.fallback:
            found.update(transport_fallback=self.transport, transport_fallbacks=1)
        return found


class ArticleAccess:
    """Baixa a página da notícia, trocando de transporte quando o site recusa (403).

    Seguro para várias threads. Cada leitor é fechado antes de o próximo abrir: o leitor do motor
    segura a vaga de acesso ao host, e duas vagas abertas ao mesmo tempo se bloqueariam.
    """

    def __init__(
        self,
        fallback: tuple[str, ...] = FALLBACK_TRANSPORTS,
        remember: int = REMEMBERED,
        pause: float = REST_SECONDS,
        refusals: int = REFUSALS_TO_REST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``remember``: sites com transporte aprendido guardados (os mais novos).

        ``refusals`` recusas totais seguidas põem o site em descanso por ``pause`` segundos.
        """
        self._fallback = fallback
        self._remember = remember
        self._pause, self._refusals, self._clock = pause, refusals, clock
        self._known: OrderedDict[str, str] = OrderedDict()
        self._refused: dict[str, int] = {}
        self._resting: dict[str, float] = {}
        self._lock = threading.Lock()

    def get(self, url: str, base: str, open_reader: OpenReader) -> Fetched:
        """Lê ``url``; ``base`` é o transporte do link e ``open_reader`` abre um leitor dele."""
        host = (urlsplit(url).hostname or "").lower()
        first = self._learned(host) or base
        if self._is_resting(host):
            return Fetched(FetchResult(url, 403, b"", None, url), _NoReader(first), first, ())
        fetched = self._read(url, first, open_reader)
        if (
            fetched.response.ok
            or fetched.response.status not in REFUSED
            or getattr(fetched.reader, "cooldown_deferred", False)
        ):
            if fetched.response.ok:
                self._opened(host)
            return fetched
        attempts = [first]
        for transport in self._fallback:
            if transport == first:
                continue
            attempts.append(transport)
            candidate = self._read(url, transport, open_reader)
            if candidate.response.ok:
                self._learn(host, transport)
                self._opened(host)
                return replace(candidate, attempts=tuple(attempts), fallback=True)
            if candidate.response.status not in REFUSED:
                break  # erro de outra natureza (404, 5xx...): não é questão de transporte
        else:
            self._refusal(host)  # todos os transportes recusaram (403)
        if first != base:
            self._forget(host)
        return replace(fetched, attempts=tuple(attempts))

    @staticmethod
    def _read(url: str, transport: str, open_reader: OpenReader) -> Fetched:
        with open_reader(transport) as reader:
            response = reader.get(url)
        return Fetched(response, reader, transport, (transport,))

    def _learned(self, host: str) -> str | None:
        with self._lock:
            return self._known.get(host)

    def _learn(self, host: str, transport: str) -> None:
        with self._lock:
            self._known[host] = transport
            self._known.move_to_end(host)
            while len(self._known) > self._remember:
                self._known.popitem(last=False)

    def _forget(self, host: str) -> None:
        with self._lock:
            self._known.pop(host, None)

    def _is_resting(self, host: str) -> bool:
        with self._lock:
            until = self._resting.get(host)
            if until is None:
                return False
            if self._clock() >= until:
                del self._resting[host]
                return False
            return True

    def _refusal(self, host: str) -> None:
        with self._lock:
            self._refused[host] = self._refused.get(host, 0) + 1
            if self._refused[host] >= self._refusals:
                self._resting[host] = self._clock() + self._pause
                self._refused.pop(host, None)

    def _opened(self, host: str) -> None:
        with self._lock:
            self._refused.pop(host, None)


class _NoReader:
    """Leitor de mentira da resposta sem pedido (site em descanso): só os contadores zerados."""

    def __init__(self, transport: str) -> None:
        self.requests = 0
        self.transport = transport
        self.cooldown_deferred = False
