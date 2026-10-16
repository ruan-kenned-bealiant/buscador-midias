from buscador_midias.discovery.explore import ProbeStatus
from buscador_midias.discovery.feed_explore import explore_feed
from buscador_midias.domain.site import Site
from buscador_midias.net.fetcher import FetchResult

UA = "BuscadorMidiasBot"
HOME = "https://exemplo.com"


class FakeFetcher:
    def __init__(self, responses: dict[str, bytes | FetchResult]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        response = self.responses.get(url)
        if response is None:
            return FetchResult(url=url, status=404, content=b"", error=None)
        if isinstance(response, FetchResult):
            return response
        return FetchResult(url=url, status=200, content=response, error=None)


def make_site(url: str = HOME) -> Site:
    return Site.create(
        name="Exemplo",
        url=url,
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


def home(*links: str) -> bytes:
    head = "".join(
        f'<link rel="alternate" type="application/rss+xml" href="{href}">' for href in links
    )
    return f"<html><head>{head}</head><body></body></html>".encode()


def rss(*urls: str) -> bytes:
    items = "".join(f"<item><title>T {u}</title><link>{u}</link></item>" for u in urls)
    return f'<rss version="2.0"><channel>{items}</channel></rss>'.encode()


def blocked(url: str, status: int = 403) -> FetchResult:
    return FetchResult(url=url, status=status, content=b"", error=None)


def test_le_o_feed_declarado_na_home():
    fetcher = FakeFetcher(
        {
            f"{HOME}/": b"",
            HOME: home("/feed/"),
            f"{HOME}/feed/": rss(f"{HOME}/a-materia-um", f"{HOME}/a-materia-dois"),
        }
    )

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert result.status is ProbeStatus.OK
    assert [e.url for e in result.entries] == [f"{HOME}/a-materia-um", f"{HOME}/a-materia-dois"]
    assert f"{HOME}/rss.xml" not in fetcher.calls  # declarado: não adivinha


def test_sem_feed_declarado_adivinha_os_caminhos_e_para_no_primeiro_que_serve():
    fetcher = FakeFetcher({HOME: home(), f"{HOME}/rss.xml": rss(f"{HOME}/materia-x")})

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert result.status is ProbeStatus.OK
    assert [e.url for e in result.entries] == [f"{HOME}/materia-x"]
    assert f"{HOME}/rss" not in fetcher.calls


def test_varios_feeds_declarados_somam_sem_repetir():
    fetcher = FakeFetcher(
        {
            HOME: home("/politica.xml", "/economia.xml", "/politica.xml"),
            f"{HOME}/politica.xml": rss(f"{HOME}/p-um", f"{HOME}/comum"),
            f"{HOME}/economia.xml": rss(f"{HOME}/comum", f"{HOME}/e-um"),
        }
    )

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert [e.url for e in result.entries] == [f"{HOME}/p-um", f"{HOME}/comum", f"{HOME}/e-um"]


def test_so_aceita_itens_que_pertencem_a_fonte():
    fetcher = FakeFetcher(
        {
            HOME: home("/feed/"),
            f"{HOME}/feed/": rss(f"{HOME}/da-fonte", "https://outro.com/de-fora"),
        }
    )

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert [e.url for e in result.entries] == [f"{HOME}/da-fonte"]


def test_fonte_de_secao_so_aceita_itens_sob_o_prefixo():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/secao": home("/feed/"),
            f"{HOME}/feed/": rss(f"{HOME}/secao/dentro", f"{HOME}/outra/fora"),
        }
    )

    result = explore_feed(make_site("https://exemplo.com/secao"), fetcher, user_agent=UA)

    assert [e.url for e in result.entries] == [f"{HOME}/secao/dentro"]


def test_home_bloqueada_ainda_tenta_os_caminhos_adivinhados():
    fetcher = FakeFetcher({HOME: blocked(HOME), f"{HOME}/feed/": rss(f"{HOME}/passou")})

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert result.status is ProbeStatus.OK
    assert [e.url for e in result.entries] == [f"{HOME}/passou"]


def test_tudo_bloqueado_vira_bloqueio():
    fetcher = FakeFetcher(
        {
            HOME: blocked(HOME),
            f"{HOME}/feed/": blocked(f"{HOME}/feed/"),
            f"{HOME}/rss.xml": blocked(f"{HOME}/rss.xml"),
            f"{HOME}/rss": blocked(f"{HOME}/rss"),
        }
    )

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert result.status is ProbeStatus.BLOCKED
    assert result.entries == ()


def test_nenhum_feed_e_sem_bloqueio_vira_ausencia():
    fetcher = FakeFetcher({HOME: home()})

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert result.status is ProbeStatus.NO_SITEMAP
    assert "feed" in result.detail


def test_erro_de_rede_vira_erro():
    down = FetchResult(url=HOME, status=None, content=b"", error="ConnectTimeout")
    fetcher = FakeFetcher({HOME: down})

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert result.status is ProbeStatus.ERROR


def test_robots_que_proibe_a_raiz_nao_tenta_nada():
    fetcher = FakeFetcher({f"{HOME}/robots.txt": b"User-agent: *\nDisallow: /\n"})

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert result.status is ProbeStatus.ROBOTS_DISALLOWED
    assert fetcher.calls == [f"{HOME}/robots.txt"]


def test_respeita_o_robots_nos_caminhos_do_feed():
    fetcher = FakeFetcher(
        {
            f"{HOME}/robots.txt": b"User-agent: *\nDisallow: /feed/\n",
            HOME: home("/feed/"),
            f"{HOME}/rss.xml": rss(f"{HOME}/materia-y"),
        }
    )

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert f"{HOME}/feed/" not in fetcher.calls
    assert result.status is ProbeStatus.NO_SITEMAP


def test_conteudo_que_nao_e_feed_nao_conta():
    fetcher = FakeFetcher({HOME: home("/feed/"), f"{HOME}/feed/": b"<html>pagina</html>"})

    result = explore_feed(make_site(), fetcher, user_agent=UA)

    assert result.status is ProbeStatus.NO_SITEMAP


def test_feed_dado_e_lido_direto_sem_baixar_a_home():
    fetcher = FakeFetcher({f"{HOME}/meu-feed": rss(f"{HOME}/materia-x")})

    result = explore_feed(make_site(), fetcher, user_agent=UA, feed_urls=(f"{HOME}/meu-feed",))

    assert result.status is ProbeStatus.OK
    assert [e.url for e in result.entries] == [f"{HOME}/materia-x"]
    assert HOME not in fetcher.calls and f"{HOME}/feed/" not in fetcher.calls


def test_feed_dado_que_bloqueia_e_bloqueio_e_nao_adivinha_outros_caminhos():
    fetcher = FakeFetcher({f"{HOME}/meu-feed": blocked(f"{HOME}/meu-feed")})

    result = explore_feed(make_site(), fetcher, user_agent=UA, feed_urls=(f"{HOME}/meu-feed",))

    assert result.status is ProbeStatus.BLOCKED
    assert f"{HOME}/rss.xml" not in fetcher.calls
