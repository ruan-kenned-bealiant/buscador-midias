"""Orçamento por fonte, compartilhado entre os mecanismos de descoberta."""

from collections.abc import Callable
from time import monotonic

from buscador_midias.net.fetcher import Fetcher, FetchResult


class FilteredFetcher:
    """Aplica escopo e exclusões a cada endereço, inclusive filhos de sitemaps."""

    def __init__(self, inner: Fetcher, allowed: Callable[[str], bool]) -> None:
        """Configura o filtro sem acrescentar chamadas HTTP."""
        self.inner = inner
        self.allowed = allowed

    def get(self, url: str) -> FetchResult:
        """Recusa endereços inválidos ou fora do escopo antes do transporte."""
        try:
            permitted = self.allowed(url)
        except ValueError:
            permitted = False
        if not permitted:
            return FetchResult(url, None, b"", "endereço fora do escopo ou excluído manualmente")
        return self.inner.get(url)


class BudgetFetcher:
    """Recusa novas chamadas após o limite, preservando respostas já processadas.

    O tempo é conferido antes da chamada; uma operação em andamento usa os timeouts
    da pilha HTTP. Redirecionamentos e retentativas internos ficam à parte da contagem.
    """

    def __init__(
        self,
        inner: Fetcher,
        *,
        max_requests: int,
        seconds: float,
        allowed: Callable[[str], bool],
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """Define saldo, prazo e escopo; zero desliga o teto correspondente."""
        if max_requests < 0 or not 0 <= seconds < float("inf"):
            raise ValueError("orçamentos de descoberta precisam ser não negativos e finitos")
        self.inner = FilteredFetcher(inner, allowed)
        self.max_requests = max_requests
        self.deadline = clock() + seconds if seconds else float("inf")
        self.clock = clock
        self.allowed = allowed
        self.requests = 0
        self.exhausted = False

    def get(self, url: str) -> FetchResult:
        """Aplica escopo/exclusões e saldo antes de chamar a pilha HTTP."""
        try:
            permitted = self.allowed(url)
        except ValueError:
            permitted = False
        if not permitted:
            return FetchResult(url, None, b"", "endereço fora do escopo ou excluído manualmente")
        if (
            self.max_requests
            and self.requests >= self.max_requests
            or self.clock() >= self.deadline
        ):
            self.exhausted = True
            return FetchResult(
                url, None, b"", "limite total de descoberta atingido; leitura parcial"
            )
        self.requests += 1
        return self.inner.get(url)
