import httpx
import pytest

from buscador_midias.net.stack import MissingCredentialsError, build_fetcher

ENV = {"BRIGHTDATA_API_KEY": "k", "BRIGHTDATA_ZONE_UNLOCKER": "z"}


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_sem_bright_data_nao_ha_fallback():
    stack = build_fetcher(
        client=make_client(lambda r: httpx.Response(403)),
        interval=0,
        bright_data=False,
        env={},
        max_fallback_requests=10,
    )

    assert stack.fallback is None
    assert stack.fetcher.get("https://alvo.com/robots.txt").status == 403


@pytest.mark.parametrize(
    "env", [{}, {"BRIGHTDATA_API_KEY": "k"}, {"BRIGHTDATA_ZONE_UNLOCKER": "z"}]
)
def test_bright_data_sem_credenciais_levanta_erro_que_nomeia_as_variaveis(env: dict[str, str]):
    names = "BRIGHTDATA_API_KEY.*BRIGHTDATA_ZONE_UNLOCKER"
    client = make_client(lambda r: httpx.Response(200))

    with pytest.raises(MissingCredentialsError, match=names):
        build_fetcher(
            client=client,
            interval=0,
            bright_data=True,
            env=env,
            max_fallback_requests=10,
        )


def test_site_bloqueado_e_buscado_pelo_unlocker_e_o_resto_continua_direto():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        if request.url.host == "api.brightdata.com":
            return httpx.Response(200, content=b"<urlset/>")
        if request.url.host == "bloqueado.com":
            return httpx.Response(403)
        return httpx.Response(200, content=b"<urlset/>")

    stack = build_fetcher(
        client=make_client(handler),
        interval=0,
        bright_data=True,
        env=ENV,
        max_fallback_requests=10,
    )

    blocked = stack.fetcher.get("https://bloqueado.com/sitemap.xml")
    free = stack.fetcher.get("https://livre.com/sitemap.xml")

    assert blocked.ok
    assert free.ok
    assert calls == [
        "GET https://bloqueado.com/sitemap.xml",
        "POST https://api.brightdata.com/request",
        "GET https://livre.com/sitemap.xml",
    ]
    assert stack.fallback is not None
    assert stack.fallback.fallback_requests == 1
    assert stack.fallback.fallback_hosts == ("bloqueado.com",)


def test_hosts_bloqueados_de_rodadas_anteriores_nao_tentam_o_acesso_gratuito():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.host}")
        return httpx.Response(200, content=b"<urlset/>")

    stack = build_fetcher(
        client=make_client(handler),
        interval=0,
        bright_data=True,
        env=ENV,
        max_fallback_requests=10,
        known_fallback_hosts=["bloqueado.com"],
    )

    stack.fetcher.get("https://bloqueado.com/sitemap.xml")

    assert calls == ["POST api.brightdata.com"]


def test_base_url_do_unlocker_vem_do_ambiente():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(403 if request.url.host == "alvo.com" else 200, content=b"<urlset/>")

    stack = build_fetcher(
        client=make_client(handler),
        interval=0,
        bright_data=True,
        env={**ENV, "BRIGHTDATA_BASE_URL": "https://proxy.interno"},
        max_fallback_requests=10,
    )

    stack.fetcher.get("https://alvo.com/sitemap.xml")

    assert calls[-1] == "https://proxy.interno/request"


def test_limite_de_bytes_do_corpo_e_configuravel_na_pilha_direta():
    big = httpx.Response(200, content=b"x" * 5000)
    stack = build_fetcher(
        client=make_client(lambda r: big),
        interval=0,
        bright_data=False,
        env={},
        max_fallback_requests=0,
        max_bytes=1000,
    )

    result = stack.fetcher.get("https://alvo.com/materia")

    assert result.error is not None and "limite" in result.error


def test_unlocker_sozinho_exige_as_credenciais_e_respeita_o_teto():
    from buscador_midias.net.stack import build_unlocker

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("x")
        return httpx.Response(200, content=b"<html>ok</html>")

    with pytest.raises(MissingCredentialsError, match="BRIGHTDATA_API_KEY"):
        build_unlocker(make_client(handler), {}, max_requests=1)

    unlocker = build_unlocker(make_client(handler), ENV, max_requests=1)

    assert unlocker.get("https://a.com/x").status == 200
    assert unlocker.get("https://a.com/y").status is None
    assert len(calls) == 1
    assert unlocker.requests_made == 1
