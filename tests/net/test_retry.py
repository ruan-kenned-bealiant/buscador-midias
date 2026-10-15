from buscador_midias.net.fetcher import FetchResult, RetryingFetcher


def ok() -> FetchResult:
    return FetchResult(url="u", status=200, content=b"x", error=None)


def http(status: int) -> FetchResult:
    return FetchResult(url="u", status=status, content=b"", error=None)


def failure(error: str) -> FetchResult:
    return FetchResult(url="u", status=None, content=b"", error=error)


class ScriptedFetcher:
    def __init__(self, *results: FetchResult) -> None:
        self.results = list(results)
        self.calls = 0

    def get(self, url: str) -> FetchResult:
        self.calls += 1
        return self.results.pop(0)


def make(inner: ScriptedFetcher, attempts: int = 3) -> tuple[RetryingFetcher, list[float]]:
    sleeps: list[float] = []
    return RetryingFetcher(inner, attempts=attempts, backoff=2.0, sleep=sleeps.append), sleeps


def test_sucesso_na_primeira_nao_dorme():
    inner = ScriptedFetcher(ok())
    fetcher, sleeps = make(inner)

    assert fetcher.get("u").ok
    assert inner.calls == 1
    assert sleeps == []


def test_timeout_e_repetido_ate_dar_certo():
    inner = ScriptedFetcher(failure("ReadTimeout: The read operation timed out"), ok())
    fetcher, sleeps = make(inner)

    assert fetcher.get("u").ok
    assert inner.calls == 2
    assert sleeps == [2.0]


def test_5xx_transitorio_usa_espera_exponencial():
    inner = ScriptedFetcher(http(503), http(502), ok())
    fetcher, sleeps = make(inner)

    assert fetcher.get("u").ok
    assert sleeps == [2.0, 4.0]


def test_esgotadas_as_tentativas_devolve_a_ultima_resposta():
    inner = ScriptedFetcher(http(503), http(503), http(504))
    fetcher, _ = make(inner)

    result = fetcher.get("u")

    assert result.status == 504
    assert inner.calls == 3


def test_nao_repete_erros_permanentes():
    for result in (
        http(404),
        http(403),
        http(500),
        failure("ConnectError: [Errno -3] Temporary failure in name resolution"),
        failure("ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
        failure("corpo acima do limite de 100 bytes"),
    ):
        inner = ScriptedFetcher(result)
        fetcher, sleeps = make(inner)

        fetcher.get("u")

        assert inner.calls == 1, result
        assert sleeps == []
