import threading
import time

from buscador_midias.net.fetcher import FetchResult
from buscador_midias.net.memoizing import MemoizingFetcher


class CountingFetcher:
    """Fake que conta as chamadas por URL."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[str] = []
        self._delay = delay
        self._lock = threading.Lock()

    def get(self, url: str) -> FetchResult:
        with self._lock:
            self.calls.append(url)
        time.sleep(self._delay)
        return FetchResult(url, 200, b"corpo", None)


def test_repete_a_resposta_sem_nova_requisicao_nos_hosts_compartilhados():
    inner = CountingFetcher()
    fetcher = MemoizingFetcher(inner, hosts={"g1.globo.com"})

    first = fetcher.get("https://g1.globo.com/sitemap/g1/sitemap.xml")
    second = fetcher.get("https://g1.globo.com/sitemap/g1/sitemap.xml")

    assert first == second
    assert inner.calls == ["https://g1.globo.com/sitemap/g1/sitemap.xml"]


def test_hosts_fora_da_lista_nao_ficam_em_memoria():
    inner = CountingFetcher()
    fetcher = MemoizingFetcher(inner, hosts={"g1.globo.com"})

    fetcher.get("https://exemplo.com/sitemap.xml")
    fetcher.get("https://exemplo.com/sitemap.xml")

    assert len(inner.calls) == 2


def test_host_com_www_conta_como_o_host_da_lista():
    inner = CountingFetcher()
    fetcher = MemoizingFetcher(inner, hosts={"g1.globo.com"})

    fetcher.get("https://www.g1.globo.com/robots.txt")
    fetcher.get("https://www.g1.globo.com/robots.txt")

    assert len(inner.calls) == 1


def test_urls_diferentes_do_mesmo_host_sao_baixadas_cada_uma():
    inner = CountingFetcher()
    fetcher = MemoizingFetcher(inner, hosts={"g1.globo.com"})

    fetcher.get("https://g1.globo.com/robots.txt")
    fetcher.get("https://g1.globo.com/sitemap.xml")

    assert len(inner.calls) == 2


def test_threads_simultaneas_na_mesma_url_baixam_uma_vez():
    inner = CountingFetcher(delay=0.05)
    fetcher = MemoizingFetcher(inner, hosts={"g1.globo.com"})
    url = "https://g1.globo.com/sitemap/g1/sitemap.xml"

    threads = [threading.Thread(target=fetcher.get, args=(url,)) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert inner.calls == [url]
