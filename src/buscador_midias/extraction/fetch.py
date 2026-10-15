"""Download direto de artigos: educado (robots.txt, intervalo por host) e sem fallback pago."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.net.fetcher import Fetcher, FetchResult

_BLOCKING_STATUSES = frozenset({401, 403, 429})
_GONE_STATUSES = frozenset({404, 410})
_HTTP_SCHEMES = ("http", "https")


class PageStatus(StrEnum):
    """Desfecho do download de um artigo."""

    OK = "ok"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"
    ROBOTS_DISALLOWED = "robots_disallowed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PageFetch:
    """Resultado do download de um artigo.

    Attributes:
        url: URL pedida.
        status: Desfecho.
        http_status: Status HTTP (``None`` se não houve resposta ou o robots.txt impediu).
        content: Corpo recebido (vazio fora do ``OK``).
        content_type: Cabeçalho ``Content-Type``.
        final_url: URL final depois de redirecionamentos.
        error: Explicação curta quando não deu certo.
    """

    url: str
    status: PageStatus
    http_status: int | None = None
    content: bytes = b""
    content_type: str | None = None
    final_url: str | None = None
    error: str | None = None


class ArticleFetcher:
    """Baixa artigos respeitando o ``robots.txt`` de cada site (lido uma vez e guardado).

    Seguro para várias threads. Não usa Bright Data: quem decide o que fazer com um site que
    bloqueia (analisar só pela manchete, ou pagar a captura) é a camada acima.
    """

    def __init__(self, fetcher: Fetcher, *, user_agent: str) -> None:
        """Cria o baixador.

        Args:
            fetcher: Porta HTTP (use a pilha com intervalo por host).
            user_agent: Nome do nosso agente, usado nas regras do robots.txt.
        """
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._registry_lock = threading.Lock()
        self._origin_locks: dict[str, threading.Lock] = {}
        self._robots: dict[str, RobotsPolicy | PageFetch] = {}

    def fetch(self, url: str) -> PageFetch:
        """Baixa o artigo, se o robots.txt do site permitir.

        Args:
            url: URL da matéria.

        Returns:
            O resultado; nunca levanta por causa da rede ou do conteúdo do site.
        """
        parts = urlsplit(url)
        if parts.scheme not in _HTTP_SCHEMES or not parts.hostname:
            return PageFetch(url, PageStatus.ERROR, error="URL inválida")
        origin = f"{parts.scheme}://{parts.netloc}"

        policy = self._policy(origin)
        if isinstance(policy, PageFetch):
            return PageFetch(url, policy.status, policy.http_status, error=policy.error)
        if not policy.allows(url, self._user_agent):
            return PageFetch(url, PageStatus.ROBOTS_DISALLOWED, error="robots.txt proíbe o acesso")
        return _classify(url, self._fetcher.get(url))

    def _policy(self, origin: str) -> RobotsPolicy | PageFetch:
        """Política do site (ou o motivo de não podermos baixar), lida uma única vez."""
        with self._registry_lock:
            lock = self._origin_locks.setdefault(origin, threading.Lock())
        with lock:
            if origin not in self._robots:
                self._robots[origin] = self._read_robots(origin)
            return self._robots[origin]

    def _read_robots(self, origin: str) -> RobotsPolicy | PageFetch:
        """Baixa e interpreta o robots.txt; 404 libera tudo, bloqueio ou falha impedem o acesso."""
        url = f"{origin}/robots.txt"
        result = self._fetcher.get(url)
        if result.ok:
            return RobotsPolicy.parse(
                result.content.decode("utf-8", errors="replace"), base_url=origin
            )
        if result.status in _GONE_STATUSES:
            return RobotsPolicy.allow_all()
        failed = _classify(url, result)
        return PageFetch(
            url, failed.status, failed.http_status, error=f"robots.txt: {failed.error}"
        )


def _classify(url: str, result: FetchResult) -> PageFetch:
    """Traduz a resposta HTTP em um desfecho."""
    if result.ok:
        return PageFetch(
            url,
            PageStatus.OK,
            result.status,
            result.content,
            result.content_type,
            result.final_url or url,
        )
    if result.status in _BLOCKING_STATUSES:
        return PageFetch(url, PageStatus.BLOCKED, result.status, error=f"HTTP {result.status}")
    if result.status in _GONE_STATUSES:
        return PageFetch(url, PageStatus.NOT_FOUND, result.status, error=f"HTTP {result.status}")
    return PageFetch(
        url,
        PageStatus.ERROR,
        result.status,
        error=result.error or f"HTTP {result.status}",
    )
