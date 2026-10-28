"""HTTP gratuito com orçamento de leituras e robots por origem para o mapeamento."""

from urllib.parse import urlsplit

from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.domain.site import Site
from buscador_midias.net.fetcher import Fetcher, FetchResult

ORIGIN_FAILURE_LIMIT = 3
_TRANSPORT_ERRORS = (
    "ConnectError:",
    "ConnectTimeout:",
    "ReadTimeout:",
    "ReadError:",
    "WriteError:",
    "WriteTimeout:",
    "RemoteProtocolError:",
    "PoolTimeout:",
    "ProxyError:",
)


def is_transport_failure(result: FetchResult) -> bool:
    """Falhas de conexão/origem; 404, proibições e orçamento não pausam outras páginas."""
    return result.status is None and any(t in (result.error or "") for t in _TRANSPORT_ERRORS)


class MappingFetcher:
    """Aplica robots a cada host e conta chamadas à pilha HTTP, inclusive sua leitura.

    Cada instância pertence a uma fonte e uma thread. A pilha externa mantém o intervalo
    por host e suas retentativas internas. Uma falha em robots impede a leitura de conteúdo.
    """

    def __init__(self, inner: Fetcher, site: Site, user_agent: str, max_requests: int) -> None:
        """Configura o orçamento (zero sem teto) e os hosts que podem ser consultados."""
        self.inner = inner
        self.site = site
        self.user_agent = user_agent
        self.max_requests = max_requests
        self.requests = 0
        self.exhausted = False
        self._policies: dict[str, RobotsPolicy | FetchResult] = {}

    def seed_policy(self, url: str, policy: RobotsPolicy) -> None:
        """Reaproveita uma política já lida nesta execução, somente para sua origem."""
        parts = urlsplit(url)
        self._policies[f"{parts.scheme}://{parts.netloc}"] = policy

    def _request(self, url: str) -> FetchResult:
        """Repassa uma requisição quando ainda há saldo."""
        if self.max_requests and self.requests >= self.max_requests:
            self.exhausted = True
            return FetchResult(url, None, b"", "orçamento de mapeamento esgotado")
        self.requests += 1
        return self.inner.get(url)

    def get(self, url: str) -> FetchResult:
        """Busca o documento apenas se os robots de sua origem permitirem."""
        if not self.site.owns_host(url):
            return FetchResult(url, None, b"", "fora dos hosts da fonte")
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._policies:
            result = self._request(origin + "/robots.txt")
            if result.ok:
                self._policies[origin] = RobotsPolicy.parse(
                    result.content.decode("utf-8", errors="replace"), base_url=origin
                )
            elif result.status in (404, 410):
                self._policies[origin] = RobotsPolicy.allow_all()
            else:
                self._policies[origin] = result
        policy = self._policies[origin]
        if isinstance(policy, FetchResult):
            return FetchResult(
                url, policy.status, b"", "robots.txt: " + (policy.error or f"HTTP {policy.status}")
            )
        if not policy.allows(url, self.user_agent):
            return FetchResult(url, None, b"", "robots.txt proíbe o acesso")
        return self._request(url)
