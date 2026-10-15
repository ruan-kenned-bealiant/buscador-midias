"""Teto de chamadas para um fetcher pago (controle de custo)."""

from __future__ import annotations

import threading

from buscador_midias.net.fetcher import Fetcher, FetchResult


class LimitedFetcher:
    """Repassa ao fetcher interno até um número máximo de chamadas por execução.

    Passado o teto, devolve um erro de transporte sem chamar o fetcher interno, então nenhuma
    chamada paga extra acontece. Seguro para várias threads.
    """

    def __init__(self, inner: Fetcher, *, max_requests: int | None) -> None:
        """Cria o limitador.

        Args:
            inner: Fetcher a proteger (ex.: Bright Data).
            max_requests: Máximo de chamadas (``None``: sem teto).
        """
        self._inner = inner
        self._max = max_requests
        self._lock = threading.Lock()
        self._made = 0

    @property
    def requests_made(self) -> int:
        """Quantas chamadas foram repassadas ao fetcher interno."""
        return self._made

    def get(self, url: str) -> FetchResult:
        """Repassa a chamada se ainda há saldo; senão devolve erro de limite."""
        with self._lock:
            if self._max is not None and self._made >= self._max:
                return FetchResult(
                    url, None, b"", f"limite de {self._max} chamadas pagas atingido nesta execução"
                )
            self._made += 1
        return self._inner.get(url)
