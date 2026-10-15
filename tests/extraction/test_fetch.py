import threading

from buscador_midias.extraction.fetch import ArticleFetcher, PageStatus
from buscador_midias.net.fetcher import FetchResult

UA = "BuscadorMidiasBot"


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchResult | bytes]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def get(self, url: str) -> FetchResult:
        with self._lock:
            self.calls.append(url)
        response = self.responses.get(url)
        if response is None:
            return FetchResult(url=url, status=404, content=b"", error=None)
        if isinstance(response, bytes):
            return FetchResult(
                url=url,
                status=200,
                content=response,
                error=None,
                final_url=url,
                content_type="text/html",
            )
        return response


def result(
    url: str, status: int | None, content: bytes = b"", error: str | None = None, **kw
) -> FetchResult:
    return FetchResult(url=url, status=status, content=content, error=error, **kw)


ARTICLE = "https://a.com/politica/pf-prende-empresario"


def test_artigo_baixado_traz_corpo_tipo_e_url_final():
    fetcher = FakeFetcher(
        {"https://a.com/robots.txt": b"User-agent: *\nAllow: /\n", ARTICLE: b"<html>ok</html>"}
    )

    page = ArticleFetcher(fetcher, user_agent=UA).fetch(ARTICLE)

    assert page.status is PageStatus.OK
    assert page.content == b"<html>ok</html>"
    assert page.http_status == 200
    assert page.final_url == ARTICLE
    assert page.content_type == "text/html"


def test_robots_que_proibe_impede_o_download():
    fetcher = FakeFetcher({"https://a.com/robots.txt": b"User-agent: *\nDisallow: /politica/\n"})

    page = ArticleFetcher(fetcher, user_agent=UA).fetch(ARTICLE)

    assert page.status is PageStatus.ROBOTS_DISALLOWED
    assert ARTICLE not in fetcher.calls


def test_robots_e_baixado_uma_vez_por_site_mesmo_com_varios_artigos():
    fetcher = FakeFetcher({ARTICLE: b"<html>1</html>", "https://a.com/outra": b"<html>2</html>"})
    articles = ArticleFetcher(fetcher, user_agent=UA)

    articles.fetch(ARTICLE)
    articles.fetch("https://a.com/outra")

    assert fetcher.calls.count("https://a.com/robots.txt") == 1


def test_robots_ausente_libera_tudo():
    fetcher = FakeFetcher({ARTICLE: b"<html>ok</html>"})  # robots.txt devolve 404

    assert ArticleFetcher(fetcher, user_agent=UA).fetch(ARTICLE).status is PageStatus.OK


def test_robots_bloqueado_ou_com_falha_nao_baixa_o_artigo():
    blocked = FakeFetcher(
        {"https://a.com/robots.txt": result("https://a.com/robots.txt", 403), ARTICLE: b"x"}
    )
    broken = FakeFetcher(
        {
            "https://a.com/robots.txt": result(
                "https://a.com/robots.txt", None, error="ReadTimeout: x"
            )
        }
    )

    assert ArticleFetcher(blocked, user_agent=UA).fetch(ARTICLE).status is PageStatus.BLOCKED
    assert ArticleFetcher(broken, user_agent=UA).fetch(ARTICLE).status is PageStatus.ERROR
    assert ARTICLE not in blocked.calls
    assert ARTICLE not in broken.calls


def test_status_do_artigo_vira_um_desfecho():
    robots = {"https://a.com/robots.txt": b""}
    cases = {
        401: PageStatus.BLOCKED,
        403: PageStatus.BLOCKED,
        429: PageStatus.BLOCKED,
        404: PageStatus.NOT_FOUND,
        410: PageStatus.NOT_FOUND,
        500: PageStatus.ERROR,
        503: PageStatus.ERROR,
    }
    for status, expected in cases.items():
        fetcher = FakeFetcher({**robots, ARTICLE: result(ARTICLE, status)})

        page = ArticleFetcher(fetcher, user_agent=UA).fetch(ARTICLE)

        assert page.status is expected, status
        assert page.http_status == status


def test_falha_de_rede_no_artigo_vira_erro_com_mensagem():
    fetcher = FakeFetcher(
        {"https://a.com/robots.txt": b"", ARTICLE: result(ARTICLE, None, error="ConnectError: x")}
    )

    page = ArticleFetcher(fetcher, user_agent=UA).fetch(ARTICLE)

    assert page.status is PageStatus.ERROR
    assert page.http_status is None
    assert "ConnectError" in (page.error or "")


def test_robots_de_sites_diferentes_nao_se_misturam():
    fetcher = FakeFetcher(
        {
            "https://a.com/robots.txt": b"User-agent: *\nDisallow: /\n",
            "https://b.com/robots.txt": b"",
            "https://b.com/x": b"<html>ok</html>",
        }
    )
    articles = ArticleFetcher(fetcher, user_agent=UA)

    assert articles.fetch("https://a.com/x").status is PageStatus.ROBOTS_DISALLOWED
    assert articles.fetch("https://b.com/x").status is PageStatus.OK


def test_downloads_concorrentes_do_mesmo_site_pedem_o_robots_uma_unica_vez():
    fetcher = FakeFetcher({f"https://a.com/m{n}": b"<html>ok</html>" for n in range(8)})
    articles = ArticleFetcher(fetcher, user_agent=UA)
    threads = [
        threading.Thread(target=articles.fetch, args=(f"https://a.com/m{n}",)) for n in range(8)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert fetcher.calls.count("https://a.com/robots.txt") == 1


def test_url_invalida_vira_erro_sem_levantar():
    page = ArticleFetcher(FakeFetcher({}), user_agent=UA).fetch("nao-e-url")

    assert page.status is PageStatus.ERROR
