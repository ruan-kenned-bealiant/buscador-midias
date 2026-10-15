"""Política de fallback: quando e como recorrer a um fetcher pago para sites que bloqueiam."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from buscador_midias.net.fetcher import Fetcher, FetchResult, host_key

_BLOCKING_STATUSES = frozenset({401, 403, 429})
_HTML_PREFIXES = (b"<!doctype html", b"<html")
_XML_SUFFIXES = (".xml", ".xml.gz", "robots.txt")


def looks_blocked(url: str, result: FetchResult) -> bool:
    """Indica se a resposta parece um bloqueio ao nosso agente.

    É bloqueio: HTTP 401/403/429, ou uma página HTML com status 200 no lugar de algo que deveria
    ser XML ou robots.txt (desafio de bot e 404 "suave" voltam assim). Falhas de rede, 404 e 5xx
    não contam: repetir pelo fallback pago não resolveria.

    Args:
        url: URL requisitada (define se era esperado XML).
        result: Resposta obtida pelo fetcher primário.

    Returns:
        ``True`` se vale tentar o fallback.
    """
    if result.error is None and result.status in _BLOCKING_STATUSES:
        return True
    return result.ok and _expects_xml(url) and _is_html(result.content)


def _expects_xml(url: str) -> bool:
    """A URL aponta para sitemap/XML ou robots.txt, nunca para uma página de notícia."""
    path = urlsplit(url).path.lower()
    return path.endswith(_XML_SUFFIXES) or "sitemap" in path


def _is_html(content: bytes) -> bool:
    """O conteúdo começa como um documento HTML."""
    return content.lstrip()[:15].lower().startswith(_HTML_PREFIXES)


class FallbackFetcher:
    """Usa um fetcher pago só para hosts que bloqueiam o fetcher gratuito, com teto de uso.

    O primeiro bloqueio de um host o marca como bloqueado: as requisições seguintes a ele vão
    direto ao fallback, sem desperdiçar tentativas nem insistir em quem já nos recusou. O teto de
    requisições evita que um erro de configuração ou um catálogo ruim gere custo sem limite; ao
    esgotá-lo, o resultado do primário (o bloqueio) é devolvido.
    """

    def __init__(
        self,
        primary: Fetcher,
        fallback: Fetcher,
        *,
        is_blocked: Callable[[str, FetchResult], bool] = looks_blocked,
        max_fallback_requests: int | None = None,
        initial_blocked_hosts: Iterable[str] = (),
    ) -> None:
        """Cria o decorador.

        Args:
            primary: Fetcher gratuito, tentado primeiro.
            fallback: Fetcher pago, usado quando o primário é bloqueado.
            is_blocked: Critério de bloqueio (injetável).
            max_fallback_requests: Teto de chamadas ao fallback; ``None`` = sem teto.
            initial_blocked_hosts: Hosts já conhecidos como bloqueados (de rodadas anteriores);
                vão direto ao fallback, sem tentar o acesso gratuito.
        """
        self._primary = primary
        self._fallback = fallback
        self._is_blocked = is_blocked
        self._max = max_fallback_requests
        self._lock = threading.Lock()
        self._blocked_hosts: set[str] = {
            host.lower().removeprefix("www.") for host in initial_blocked_hosts
        }
        self._fallback_requests = 0

    @property
    def fallback_requests(self) -> int:
        """Quantas requisições foram enviadas ao fallback."""
        return self._fallback_requests

    @property
    def fallback_hosts(self) -> tuple[str, ...]:
        """Hosts que passaram a depender do fallback, em ordem alfabética."""
        with self._lock:
            return tuple(sorted(self._blocked_hosts))

    def get(self, url: str) -> FetchResult:
        """Busca a URL pelo primário e, se for bloqueio, pelo fallback (respeitando o teto)."""
        host = host_key(url)
        with self._lock:
            known_blocked = host in self._blocked_hosts

        if known_blocked and self._reserve():
            return self._fallback.get(url)

        result = self._primary.get(url)
        if known_blocked or not self._is_blocked(url, result):
            return result

        with self._lock:
            self._blocked_hosts.add(host)
        return self._fallback.get(url) if self._reserve() else result

    def _reserve(self) -> bool:
        """Reserva uma chamada ao fallback; ``False`` se o teto já foi atingido."""
        with self._lock:
            if self._max is not None and self._fallback_requests >= self._max:
                return False
            self._fallback_requests += 1
            return True
