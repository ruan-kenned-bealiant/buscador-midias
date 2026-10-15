import pytest

from buscador_midias.net.fallback import FallbackFetcher, looks_blocked
from buscador_midias.net.fetcher import FetchResult

HTML = b"<!DOCTYPE html><html><body>Just a moment...</body></html>"
XML = b"<urlset/>"


def result(status: int | None, content: bytes = b"", error: str | None = None) -> FetchResult:
    return FetchResult(url="u", status=status, content=content, error=error)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_status_de_bloqueio_e_bloqueado(status: int):
    assert looks_blocked("https://a.com/sitemap.xml", result(status))


@pytest.mark.parametrize("status", [404, 410, 500, 503])
def test_outros_status_nao_acionam_o_fallback(status: int):
    assert not looks_blocked("https://a.com/sitemap.xml", result(status))


def test_falha_de_transporte_nao_aciona_o_fallback():
    assert not looks_blocked("https://a.com/sitemap.xml", result(None, error="ReadTimeout: x"))


@pytest.mark.parametrize(
    "url",
    [
        "https://a.com/sitemap.xml",
        "https://a.com/sitemap.xml.gz",
        "https://a.com/arc/outboundfeeds/sitemap/?outputType=xml",
        "https://a.com/news.xml",
        "https://a.com/robots.txt",
    ],
)
def test_html_no_lugar_de_xml_ou_robots_e_bloqueio_disfarcado(url: str):
    assert looks_blocked(url, result(200, HTML))


def test_xml_valido_nao_e_bloqueio():
    assert not looks_blocked("https://a.com/sitemap.xml", result(200, XML))


def test_html_numa_pagina_comum_e_normal():
    assert not looks_blocked("https://a.com/politica/noticia", result(200, HTML))


class Recorder:
    def __init__(self, *results: FetchResult) -> None:
        self.results = list(results)
        self.urls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.urls.append(url)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def test_nao_aciona_o_fallback_quando_o_primario_funciona():
    primary, backup = Recorder(result(200, XML)), Recorder(result(200, XML))
    fetcher = FallbackFetcher(primary, backup)

    assert fetcher.get("https://a.com/sitemap.xml").ok
    assert backup.urls == []
    assert fetcher.fallback_requests == 0


def test_aciona_o_fallback_no_bloqueio_e_devolve_a_resposta_dele():
    primary, backup = Recorder(result(403)), Recorder(result(200, XML))
    fetcher = FallbackFetcher(primary, backup)

    got = fetcher.get("https://a.com/sitemap.xml")

    assert got.ok
    assert got.content == XML
    assert backup.urls == ["https://a.com/sitemap.xml"]
    assert fetcher.fallback_requests == 1


def test_host_bloqueado_vai_direto_ao_fallback_nas_proximas():
    primary, backup = Recorder(result(403)), Recorder(result(200, XML))
    fetcher = FallbackFetcher(primary, backup)

    fetcher.get("https://a.com/robots.txt")
    fetcher.get("https://a.com/sitemap.xml")
    fetcher.get("https://www.a.com/news.xml")

    assert primary.urls == ["https://a.com/robots.txt"]
    assert len(backup.urls) == 3
    assert fetcher.fallback_hosts == ("a.com",)


def test_hosts_ja_marcados_em_rodadas_anteriores_vao_direto_ao_fallback():
    primary, backup = Recorder(result(200, XML)), Recorder(result(200, XML))
    fetcher = FallbackFetcher(primary, backup, initial_blocked_hosts=["a.com"])

    fetcher.get("https://www.a.com/robots.txt")

    assert primary.urls == []
    assert backup.urls == ["https://www.a.com/robots.txt"]
    assert fetcher.fallback_hosts == ("a.com",)


def test_outro_host_ainda_tenta_o_primario_primeiro():
    primary = Recorder(result(403), result(200, XML))
    backup = Recorder(result(200, XML))
    fetcher = FallbackFetcher(primary, backup)

    fetcher.get("https://a.com/robots.txt")
    fetcher.get("https://b.com/robots.txt")

    assert primary.urls == ["https://a.com/robots.txt", "https://b.com/robots.txt"]
    assert backup.urls == ["https://a.com/robots.txt"]


def test_html_com_status_200_em_sitemap_tambem_aciona_o_fallback():
    primary, backup = Recorder(result(200, HTML)), Recorder(result(200, XML))
    fetcher = FallbackFetcher(primary, backup)

    assert fetcher.get("https://a.com/sitemap.xml").content == XML


def test_teto_de_requisicoes_impede_novas_chamadas_ao_fallback():
    primary, backup = Recorder(result(403)), Recorder(result(200, XML))
    fetcher = FallbackFetcher(primary, backup, max_fallback_requests=1)

    first = fetcher.get("https://a.com/robots.txt")
    second = fetcher.get("https://b.com/robots.txt")

    assert first.ok
    assert second.status == 403
    assert backup.urls == ["https://a.com/robots.txt"]
    assert fetcher.fallback_requests == 1


def test_teto_esgotado_em_host_ja_marcado_volta_ao_primario():
    primary, backup = Recorder(result(403)), Recorder(result(200, XML))
    fetcher = FallbackFetcher(primary, backup, max_fallback_requests=1)

    fetcher.get("https://a.com/robots.txt")
    again = fetcher.get("https://a.com/sitemap.xml")

    assert again.status == 403
    assert primary.urls == ["https://a.com/robots.txt", "https://a.com/sitemap.xml"]
    assert len(backup.urls) == 1
