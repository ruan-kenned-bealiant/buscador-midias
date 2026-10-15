import threading

from buscador_midias.net.fetcher import FetchResult
from buscador_midias.net.limited import LimitedFetcher


class Inner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def get(self, url: str) -> FetchResult:
        with self._lock:
            self.calls.append(url)
        return FetchResult(url, 200, b"ok", None)


def test_repassa_ate_o_limite_e_depois_devolve_erro_sem_chamar_o_fetcher_interno():
    inner = Inner()
    limited = LimitedFetcher(inner, max_requests=2)

    results = [limited.get(f"https://a.com/{n}") for n in range(4)]

    assert [r.status for r in results] == [200, 200, None, None]
    assert "limite" in (results[2].error or "")
    assert inner.calls == ["https://a.com/0", "https://a.com/1"]
    assert limited.requests_made == 2


def test_limite_vale_com_varias_threads():
    inner = Inner()
    limited = LimitedFetcher(inner, max_requests=5)
    threads = [
        threading.Thread(target=limited.get, args=(f"https://a.com/{n}",)) for n in range(20)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(inner.calls) == 5
    assert limited.requests_made == 5


def test_sem_limite_repassa_tudo():
    inner = Inner()
    limited = LimitedFetcher(inner, max_requests=None)

    for n in range(30):
        limited.get(f"https://a.com/{n}")

    assert limited.requests_made == 30
