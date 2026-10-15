import json

import httpx

from buscador_midias.net.brightdata import BrightDataFetcher

API_KEY = "segredo-super-secreto"


def make(handler, **kwargs) -> BrightDataFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return BrightDataFetcher(client, api_key=API_KEY, zone="zona_unlocker", **kwargs)


def test_posta_no_endpoint_do_unlocker_com_token_zona_e_formato_raw():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"<urlset/>")

    result = make(handler).get("https://alvo.com/sitemap.xml")

    assert seen["url"] == "https://api.brightdata.com/request"
    assert seen["auth"] == f"Bearer {API_KEY}"
    assert seen["body"] == {
        "zone": "zona_unlocker",
        "url": "https://alvo.com/sitemap.xml",
        "format": "raw",
    }
    assert result.ok
    assert result.content == b"<urlset/>"
    assert result.url == "https://alvo.com/sitemap.xml"


def test_aceita_base_url_customizada_sem_barra_final():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"x")

    make(handler, base_url="https://proxy.interno/").get("https://alvo.com/")

    assert seen == ["https://proxy.interno/request"]


def test_status_de_erro_da_api_passa_adiante_sem_conteudo():
    result = make(lambda r: httpx.Response(401, content=b"unauthorized")).get("https://a.com/")

    assert result.status == 401
    assert not result.ok
    assert result.content == b""


def test_resposta_vazia_vira_erro():
    result = make(lambda r: httpx.Response(200, content=b"")).get("https://a.com/")

    assert not result.ok
    assert result.error == "resposta vazia"


def test_falha_de_transporte_nao_levanta_nem_vaza_o_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("tempo esgotado", request=request)

    result = make(handler).get("https://a.com/")

    assert result.status is None
    assert result.error is not None
    assert "ReadTimeout" in result.error
    assert API_KEY not in result.error


def test_corpo_acima_do_limite_vira_erro():
    result = make(lambda r: httpx.Response(200, content=b"a" * 500), max_bytes=100).get(
        "https://a.com/"
    )

    assert not result.ok
    assert result.error is not None
    assert "limite" in result.error


def test_conta_as_requisicoes_feitas():
    fetcher = make(lambda r: httpx.Response(200, content=b"x"))

    fetcher.get("https://a.com/1")
    fetcher.get("https://a.com/2")

    assert fetcher.requests_made == 2
