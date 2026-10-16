"""Decorador que lembra as respostas dos hosts que várias fontes compartilham."""

from __future__ import annotations

import threading
from collections.abc import Collection

from buscador_midias.net.fetcher import Fetcher, FetchResult, host_key


class MemoizingFetcher:
    """Baixa cada URL uma só vez, nos hosts indicados, durante a vida do objeto.

    Serve a sondagem: o g1 tem dez fontes (uma por editoria ou estado) que olham o mesmo índice
    de sitemap; sem isso, cada uma baixaria de novo o mesmo arquivo grande. Fica restrito aos
    hosts compartilhados para não guardar em memória os sitemaps das outras ~1.250 fontes.
    Seguro para várias threads: uma segunda thread que pede a mesma URL espera a primeira.
    """

    def __init__(self, inner: Fetcher, *, hosts: Collection[str]) -> None:
        """Cria o decorador.

        Args:
            inner: Fetcher real.
            hosts: Hosts (sem ``www.``, em minúsculas) cujas respostas ficam guardadas.
        """
        self._inner = inner
        self._hosts = frozenset(hosts)
        self._registry_lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}

    def get(self, url: str) -> FetchResult:
        """Devolve a resposta guardada ou baixa e guarda (só nos hosts compartilhados)."""
        if host_key(url) not in self._hosts:
            return self._inner.get(url)
        with self._registry_lock:
            slot = self._slots.setdefault(url, _Slot())
        with slot.lock:
            if slot.result is None:
                slot.result = self._inner.get(url)
            return slot.result


class _Slot:
    """Lugar de uma URL: a trava de quem baixa e o resultado, quando houver."""

    __slots__ = ("lock", "result")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.result: FetchResult | None = None
