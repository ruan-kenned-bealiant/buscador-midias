"""Rede compartilhada da rodada: cache limitado em memória e intervalo global por host."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from buscador_midias.catalog.reader import TestReader
from buscador_midias.catalog.store import CatalogError
from buscador_midias.net.fetcher import FetchResult


class BatchCache:
    """Até 64 MiB de respostas bem-sucedidas; falhas de conteúdo não são reutilizadas."""

    def __init__(self) -> None:
        """Inicializa caches exclusivos desta execução, sem arquivos de corpo no disco."""
        self.lock = threading.Lock()
        self.responses: OrderedDict[str, tuple[float, FetchResult]] = OrderedDict()
        self.size = 0
        self.host_next: dict[str, float] = {}
        self.hits = 0

    def wait(self, host: str) -> None:
        """Espaça requisições de todos os trabalhadores por ao menos 300 ms por host."""
        with self.lock:
            now = time.monotonic()
            start = max(now, self.host_next.get(host, now))
            self.host_next[host] = start + 0.3
        time.sleep(max(0, start - now))

    def get(self, url: str) -> FetchResult | None:
        """Retorna resposta de até cinco minutos, preservando o mesmo conteúdo observado."""
        with self.lock:
            item = self.responses.get(url)
            if item is None:
                return None
            if item[0] < time.monotonic():
                self.size -= len(item[1].content)
                del self.responses[url]
                return None
            self.responses.move_to_end(url)
            self.hits += 1
            return item[1]

    def put(self, url: str, response: FetchResult) -> None:
        """Retém somente respostas pequenas, válidas no transporte, dentro do limite global."""
        if not response.ok or len(response.content) > 512 * 1024:
            return
        with self.lock:
            old = self.responses.pop(url, None)
            if old:
                self.size -= len(old[1].content)
            self.responses[url] = (time.monotonic() + 300, response)
            self.size += len(response.content)
            while self.size > 64 * 1024 * 1024 or len(self.responses) > 8192:
                _, item = self.responses.popitem(last=False)
                self.size -= len(item[1].content)


class BatchReader(TestReader):
    """Usa as mesmas proteções de teste, com reaproveitamento explícito de respostas na rodada."""

    supports_early_stop = False

    def __init__(
        self,
        roots: list[str],
        progress: Callable[[dict[str, object]], None],
        cache: BatchCache,
        parameters: dict[str, Any],
        attempt: int,
    ) -> None:
        """Configura limites reais e força nova leitura do XML inicial nas novas tentativas."""
        super().__init__(
            roots,
            progress,
            seconds=parameters["timeout_seconds"],
            requests=parameters["max_requests"],
            max_bytes=parameters["max_response_mib"] * 1024 * 1024,
            before_request=cache.wait,
        )
        self.cache, self.attempt, self.root = cache, attempt, roots[1]
        self.cache_hits = 0

    def get(self, url: str) -> FetchResult:
        """Cache respeita o escopo de cada associação; tentativas repetem a raiz na rede."""
        self._check()
        try:
            self._scope(url)
        except CatalogError as error:
            return FetchResult(url, None, b"", str(error)[:350], final_url=url)
        cached = self.cache.get(url) if self.attempt == 1 or url != self.root else None
        if cached is not None:
            self.cache_hits += 1
            return cached
        response = super().get(url)
        self.cache.put(url, response)
        return response
