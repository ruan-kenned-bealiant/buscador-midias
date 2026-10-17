"""Orçamento total da fonte, compartilhado entre páginas, robots e mecanismos."""

from collections.abc import Callable
from time import monotonic

from buscador_midias.net.fetcher import Fetcher, FetchResult


class MappingBudgetExceededError(RuntimeError):
    """Interrompe a fonte antes de uma nova chamada, preservando as pendências."""


class MappingBudget:
    """Limita chamadas e tempo entre todos os lotes de uma fonte.

    O prazo global inclui espera na fila de workers. Uma chamada já em andamento
    termina sob os timeouts HTTP existentes; o prazo é conferido antes da próxima.
    """

    def __init__(
        self, inner: Fetcher, *, max_requests: int, max_seconds: float,
        deadline: float, clock: Callable[[], float] = monotonic,
    ) -> None:
        """Inicia o orçamento da fonte, mantendo o prazo global compartilhado."""
        self.inner = inner
        self.max_requests = max_requests
        self.clock = clock
        self.deadline = min(deadline, clock() + max_seconds)
        self.requests = 0

    def get(self, url: str) -> FetchResult:
        """Conta chamadas à pilha HTTP e interrompe antes de ultrapassar o saldo."""
        if self.clock() >= self.deadline:
            raise MappingBudgetExceededError(
                "limite de tempo do mapeamento atingido; fila preservada"
            )
        if self.requests >= self.max_requests:
            raise MappingBudgetExceededError(
                "limite total de requisições do mapeamento atingido; fila preservada"
            )
        self.requests += 1
        return self.inner.get(url)
