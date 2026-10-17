import httpx

from buscador_midias.net.fetcher import FetchResult, HttpxFetcher, ThrottledFetcher


def make_fetcher(handler, max_bytes: int = 1_000) -> HttpxFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return HttpxFetcher(client, user_agent="TesteBot/1.0", max_bytes=max_bytes)


def test_retorna_status_e_conteudo():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<urlset/>")

    result = make_fetcher(handler).get("https://exemplo.com/sitemap.xml")

    assert result.ok
    assert result.status == 200
    assert result.content == b"<urlset/>"
    assert result.error is None


def test_envia_user_agent_configurado():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, content=b"x")

    make_fetcher(handler).get("https://exemplo.com/")

    assert seen == ["TesteBot/1.0"]


def test_cookie_nao_ascii_nao_interrompe_site_e_preserva_cookies_validos():
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie", ""))
        return httpx.Response(200, content=b"ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.cookies.set("valido", "sessao-publica", domain="exemplo.com", path="/")
        client.cookies.set("invalido", "público", domain="exemplo.com", path="/")
        result = HttpxFetcher(client).get("https://exemplo.com/noticias")
        assert result.ok
        assert len(seen) == 1 and "valido=sessao-publica" in seen[0]
        assert "invalido" not in seen[0]


def test_unicode_sem_cookie_invalido_vira_falha_sem_loop_de_retentativas():
    calls = []

    def handler(request):
        calls.append(request.url)
        raise UnicodeEncodeError("ascii", "ú", 0, 1, "fora de ASCII")

    result = make_fetcher(handler).get("https://exemplo.com/noticias")
    assert not result.ok and result.error.startswith("UnicodeEncodeError:")
    assert len(calls) == 1


def test_status_de_erro_nao_e_ok_e_nao_tem_excecao():
    result = make_fetcher(lambda r: httpx.Response(404)).get("https://exemplo.com/x")

    assert not result.ok
    assert result.status == 404
    assert result.error is None


def test_falha_de_transporte_vira_resultado_sem_status():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("tempo esgotado", request=request)

    result = make_fetcher(handler).get("https://exemplo.com/x")

    assert result.status is None
    assert not result.ok
    assert result.error is not None
    assert "ConnectTimeout" in result.error


def test_corpo_acima_do_limite_vira_erro():
    big = httpx.Response(200, content=b"a" * 5_000)

    result = make_fetcher(lambda r: big, max_bytes=1_000).get("https://exemplo.com/x")

    assert result.status == 200
    assert not result.ok
    assert result.error is not None
    assert "limite" in result.error
    assert result.content == b""


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RecordingFetcher:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.urls.append(url)
        return FetchResult(url=url, status=200, content=b"", error=None)


def test_throttle_espera_o_intervalo_entre_requisicoes_do_mesmo_host():
    clock = FakeClock()
    inner = RecordingFetcher()
    fetcher = ThrottledFetcher(inner, min_interval=2.0, clock=clock.monotonic, sleep=clock.sleep)

    fetcher.get("https://exemplo.com/a")
    fetcher.get("https://exemplo.com/b")

    assert inner.urls == ["https://exemplo.com/a", "https://exemplo.com/b"]
    assert clock.sleeps == [2.0]


def test_throttle_nao_espera_entre_hosts_diferentes():
    clock = FakeClock()
    fetcher = ThrottledFetcher(
        RecordingFetcher(), min_interval=2.0, clock=clock.monotonic, sleep=clock.sleep
    )

    fetcher.get("https://a.com/x")
    fetcher.get("https://b.com/x")

    assert clock.sleeps == []


def test_throttle_desconta_o_tempo_ja_decorrido():
    clock = FakeClock()
    fetcher = ThrottledFetcher(
        RecordingFetcher(), min_interval=2.0, clock=clock.monotonic, sleep=clock.sleep
    )

    fetcher.get("https://exemplo.com/a")
    clock.now += 1.5
    fetcher.get("https://exemplo.com/b")

    assert clock.sleeps == [0.5]


def test_throttle_trata_www_e_sem_www_como_o_mesmo_host():
    clock = FakeClock()
    fetcher = ThrottledFetcher(
        RecordingFetcher(), min_interval=2.0, clock=clock.monotonic, sleep=clock.sleep
    )

    fetcher.get("https://www.exemplo.com/a")
    fetcher.get("https://exemplo.com/b")

    assert clock.sleeps == [2.0]


def test_resultado_traz_url_final_e_tipo_de_conteudo_apos_redirecionamento():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/velho":
            return httpx.Response(301, headers={"Location": "https://a.com/novo"})
        return httpx.Response(
            200, content=b"<html></html>", headers={"Content-Type": "text/html; charset=utf-8"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    result = HttpxFetcher(client).get("https://a.com/velho")

    assert result.final_url == "https://a.com/novo"
    assert result.content_type == "text/html; charset=utf-8"


def test_resultado_de_falha_nao_tem_url_final_nem_tipo():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede")

    result = HttpxFetcher(httpx.Client(transport=httpx.MockTransport(handler))).get(
        "https://a.com/x"
    )

    assert (result.final_url, result.content_type) == (None, None)
