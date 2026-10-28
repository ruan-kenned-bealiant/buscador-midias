"""Teto de chamadas pagas por site, somado entre todas as leituras daquele site.

O teto do ``BrightTestReader`` vale por leitor (um por teste); a rodada de mapeamento precisa de
um teto por site, então todos os leitores do mesmo site tiram de um único orçamento.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from buscador_midias.catalog.bright_reader import BrightTestReader
from buscador_midias.net.fetcher import FetchResult

BRIGHT_PER_SITE = 2
DIRECT_ATTEMPTS = 2


class SiteBudget:
    """Chamadas pagas que ainda restam a um site; seguro entre threads."""

    def __init__(self, limit: int) -> None:
        """Cria o orçamento; um teto negativo não faz sentido."""
        if limit < 0:
            raise ValueError("O teto de chamadas pagas não pode ser negativo.")
        self.limit = limit
        self._spent = 0
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        """Quantas chamadas pagas ainda podem ser feitas."""
        with self._lock:
            return self.limit - self._spent

    @property
    def spent(self) -> int:
        """Quantas chamadas pagas já foram feitas ou reservadas."""
        with self._lock:
            return self._spent

    def spend(self, count: int) -> None:
        """Registra gasto externo; nunca passa do teto."""
        with self._lock:
            self._spent = min(self.limit, self._spent + count)

    def reserve(self) -> bool:
        """Toma uma chamada de forma atômica; ``False`` quando o orçamento acabou."""
        with self._lock:
            if self._spent >= self.limit:
                return False
            self._spent += 1
            return True

    def refund(self) -> None:
        """Devolve uma chamada reservada e não usada."""
        with self._lock:
            self._spent = max(0, self._spent - 1)


class SiteBrightReader(BrightTestReader):
    """Leitor cujo fallback pago só acontece se o orçamento do site ainda tiver chamadas."""

    __test__ = False

    def __init__(
        self,
        budget: SiteBudget,
        roots: list[str],
        progress: Callable[[dict[str, object]], None],
        **kwargs: Any,
    ) -> None:
        """Guarda o orçamento compartilhado; cada leitura pede a sua chamada antes de pagar."""
        super().__init__(roots, progress, **kwargs)
        self.budget = budget

    def _fetch(self, url: str, *, robots: bool = False) -> FetchResult:
        before = self.bright_requests
        reserved = self.budget.reserve()
        self.max_bright_requests = before + (1 if reserved else 0)
        try:
            return super()._fetch(url, robots=robots)
        finally:
            if reserved and self.bright_requests == before:
                self.budget.refund()


def budgeted_reader(
    budget: SiteBudget,
    roots: list[str],
    progress: Callable[[dict[str, object]], None],
    **kwargs: Any,
) -> SiteBrightReader:
    """Leitor com 2 tentativas diretas antes do pago, limitado pelo orçamento do site."""
    kwargs.setdefault("direct_attempts", DIRECT_ATTEMPTS)
    return SiteBrightReader(budget, roots, progress, **kwargs)
