from datetime import UTC, datetime

from buscador_midias.discovery.probe import ProbeStatus, probe_site
from buscador_midias.domain.site import Site
from buscador_midias.net.fetcher import FetchResult

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
UA = "BuscadorMidiasBot"


class FakeFetcher:
    """Fetcher em memória: URL desconhecida responde 404."""

    def __init__(self, responses: dict[str, FetchResult | bytes]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        response = self.responses.get(url)
        if response is None:
            return FetchResult(url=url, status=404, content=b"", error=None)
        if isinstance(response, bytes):
            return FetchResult(url=url, status=200, content=response, error=None)
        return response


def status(code: int) -> FetchResult:
    return FetchResult(url="", status=code, content=b"", error=None)


def make_site(url: str = "https://exemplo.com") -> Site:
    return Site.create(
        name="Exemplo",
        url=url,
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


def urlset(*entries: tuple[str, str | None]) -> bytes:
    urls = "".join(
        f"<url><loc>{loc}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</url>"
        for loc, mod in entries
    )
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode()


def index(*children: tuple[str, str | None]) -> bytes:
    items = "".join(
        f"<sitemap><loc>{loc}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</sitemap>"
        for loc, mod in children
    )
    return (
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>'
    ).encode()


def probe(site: Site, fetcher: FakeFetcher, **kwargs: int):
    return probe_site(site, fetcher, now=NOW, user_agent=UA, **kwargs)


def test_ok_via_index_conta_urls_recentes():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"Sitemap: https://exemplo.com/index.xml\n",
            "https://exemplo.com/index.xml": index(("https://exemplo.com/s1.xml", "2026-10-15")),
            "https://exemplo.com/s1.xml": urlset(
                ("https://exemplo.com/a", "2026-10-15T08:00:00Z"),
                ("https://exemplo.com/b", "2026-10-12T08:00:00Z"),
                ("https://exemplo.com/c", "2025-01-01"),
                ("https://exemplo.com/d", None),
            ),
        }
    )

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.OK
    assert result.has_index is True
    assert result.has_news is False
    assert result.sitemap_urls == ("https://exemplo.com/index.xml",)
    assert result.urls_sampled == 4
    assert result.with_lastmod == 3
    assert result.recent_24h == 1
    assert result.recent_7d == 2


def test_fallback_para_sitemap_xml_quando_robots_nao_declara():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"User-agent: *\nDisallow: /admin\n",
            "https://exemplo.com/sitemap.xml": urlset(("https://exemplo.com/a", "2026-10-15")),
        }
    )

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.OK
    assert result.sitemap_urls == ("https://exemplo.com/sitemap.xml",)


def test_robots_ausente_ainda_tenta_os_caminhos_padrao():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/sitemap_index.xml": index(("https://exemplo.com/s.xml", None)),
            "https://exemplo.com/s.xml": urlset(("https://exemplo.com/a", "2026-10-15")),
        }
    )

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.OK
    assert "https://exemplo.com/sitemap.xml" in fetcher.calls


def test_sem_sitemap_em_lugar_nenhum():
    result = probe(make_site(), FakeFetcher({}))

    assert result.status is ProbeStatus.NO_SITEMAP
    assert result.urls_sampled == 0


def test_robots_bloqueado_marca_blocked_sem_tentar_sitemap():
    fetcher = FakeFetcher({"https://exemplo.com/robots.txt": status(403)})

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.BLOCKED
    assert fetcher.calls == ["https://exemplo.com/robots.txt"]


def test_sitemap_bloqueado_marca_blocked():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"Sitemap: https://exemplo.com/s.xml\n",
            "https://exemplo.com/s.xml": status(429),
        }
    )

    assert probe(make_site(), fetcher).status is ProbeStatus.BLOCKED


def test_falha_de_transporte_no_robots_e_erro():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": FetchResult(
                url="", status=None, content=b"", error="ConnectTimeout: tempo esgotado"
            )
        }
    )

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.ERROR
    assert "ConnectTimeout" in result.detail


def test_html_no_lugar_do_sitemap_e_erro_com_detalhe():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"Sitemap: https://exemplo.com/s.xml\n",
            "https://exemplo.com/s.xml": b"<!doctype html><html><body>oops</body></html>",
        }
    )

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.ERROR
    assert "sitemap inválido" in result.detail


def test_detecta_news_sitemap():
    news = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        b'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url>'
        b"<loc>https://exemplo.com/a</loc><news:news>"
        b"<news:publication_date>2026-10-15T09:00:00Z</news:publication_date>"
        b"<news:title>Titulo</news:title></news:news></url></urlset>"
    )
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"Sitemap: https://exemplo.com/news.xml\n",
            "https://exemplo.com/news.xml": news,
        }
    )

    result = probe(make_site(), fetcher)

    assert result.has_news is True
    assert result.recent_24h == 1
    assert result.with_lastmod == 1


def test_fonte_de_secao_conta_so_urls_sob_o_prefixo():
    fetcher = FakeFetcher(
        {
            "https://g1.globo.com/robots.txt": b"Sitemap: https://g1.globo.com/s.xml\n",
            "https://g1.globo.com/s.xml": urlset(
                ("https://g1.globo.com/globonews/a", "2026-10-15"),
                ("https://g1.globo.com/politica/b", "2026-10-15"),
            ),
        }
    )

    result = probe(make_site("https://g1.globo.com/globonews/"), fetcher)

    assert result.urls_sampled == 1


def test_indice_sem_lastmod_le_primeiro_os_ultimos_filhos():
    children = tuple((f"https://exemplo.com/s{ano}.xml", None) for ano in (1, 2, 3, 4))
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"Sitemap: https://exemplo.com/index.xml\n",
            "https://exemplo.com/index.xml": index(*children),
        }
    )

    probe(make_site(), fetcher, max_sitemap_fetches=3)

    assert fetcher.calls[2:] == ["https://exemplo.com/s4.xml", "https://exemplo.com/s3.xml"]


def test_indice_lido_mas_filhos_bloqueados_nao_e_ok():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"Sitemap: https://exemplo.com/index.xml\n",
            "https://exemplo.com/index.xml": index(("https://exemplo.com/s.xml", None)),
            "https://exemplo.com/s.xml": status(403),
        }
    )

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.BLOCKED
    assert result.urls_sampled == 0
    assert "s.xml" in result.detail


def test_indice_lido_mas_filhos_invalidos_e_erro():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"Sitemap: https://exemplo.com/index.xml\n",
            "https://exemplo.com/index.xml": index(("https://exemplo.com/s.xml", None)),
            "https://exemplo.com/s.xml": b"<!doctype html><html><body>bloqueio</body></html>",
        }
    )

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.ERROR
    assert "sitemap inválido" in result.detail


def test_urls_de_outros_hosts_sao_contadas_a_parte_e_nao_viram_erro():
    fetcher = FakeFetcher(
        {
            "https://primerahora.com/robots.txt": b"Sitemap: https://primerahora.com/s.xml\n",
            "https://primerahora.com/s.xml": urlset(
                ("https://www.elnuevodia.com/a", "2026-10-15"),
                ("https://www.elnuevodia.com/b", "2026-10-15"),
            ),
        }
    )

    result = probe(make_site("https://primerahora.com"), fetcher)

    assert result.status is ProbeStatus.OK
    assert result.urls_sampled == 0
    assert result.urls_other_hosts == 2


def test_sitemap_relativo_no_robots_e_resolvido_contra_a_origem():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"Sitemap: /mapa.xml\n",
            "https://exemplo.com/mapa.xml": urlset(("https://exemplo.com/a", "2026-10-15")),
        }
    )

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.OK
    assert result.sitemap_urls == ("https://exemplo.com/mapa.xml",)


def test_robots_so_com_sitemap_invalido_cai_nos_caminhos_padrao():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"Sitemap: hthttps://exemplo.com/s.xml\n",
            "https://exemplo.com/sitemap.xml": urlset(("https://exemplo.com/a", "2026-10-15")),
        }
    )

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.OK
    assert result.sitemap_urls == ("https://exemplo.com/sitemap.xml",)


def test_fonte_de_secao_le_primeiro_o_sitemap_da_propria_secao():
    fetcher = FakeFetcher(
        {
            "https://www.bbc.com/robots.txt": (
                b"Sitemap: https://www.bbc.com/afrique/sitemap.xml\n"
                b"Sitemap: https://www.bbc.com/portuguese/sitemap.xml\n"
            ),
            "https://www.bbc.com/portuguese/sitemap.xml": urlset(
                ("https://www.bbc.com/portuguese/artigo", "2026-10-15")
            ),
        }
    )

    result = probe(make_site("https://www.bbc.com/portuguese"), fetcher, max_sitemap_fetches=1)

    assert fetcher.calls == [
        "https://www.bbc.com/robots.txt",
        "https://www.bbc.com/portuguese/sitemap.xml",
    ]
    assert result.urls_sampled == 1


def test_fonte_de_secao_prefere_filho_de_indice_com_o_nome_da_secao():
    fetcher = FakeFetcher(
        {
            "https://www.dw.com/robots.txt": b"Sitemap: https://www.dw.com/index.xml\n",
            "https://www.dw.com/index.xml": index(
                ("https://www.dw.com/en/s.xml", "2026-10-15"),
                ("https://www.dw.com/pt-br/s.xml", "2026-01-01"),
            ),
            "https://www.dw.com/pt-br/s.xml": urlset(
                ("https://www.dw.com/pt-br/materia", "2026-10-15")
            ),
        }
    )

    result = probe(make_site("https://www.dw.com/pt-br"), fetcher, max_sitemap_fetches=2)

    assert result.urls_sampled == 1


def test_dominio_inteiro_nao_usa_prioridade_de_secao():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": (
                b"Sitemap: https://exemplo.com/b.xml\nSitemap: https://exemplo.com/a.xml\n"
            ),
        }
    )

    probe(make_site(), fetcher, max_sitemap_fetches=2)

    assert fetcher.calls[1:] == ["https://exemplo.com/b.xml", "https://exemplo.com/a.xml"]


def test_conta_urls_publicadas_em_subdominio_da_fonte():
    fetcher = FakeFetcher(
        {
            "https://folha.uol.com.br/robots.txt": b"Sitemap: https://folha.uol.com.br/s.xml\n",
            "https://folha.uol.com.br/s.xml": urlset(
                ("https://www1.folha.uol.com.br/mundo/a.shtml", "2026-10-15"),
                ("https://outro.com.br/b", "2026-10-15"),
            ),
        }
    )

    result = probe(make_site("https://folha.uol.com.br"), fetcher)

    assert result.urls_sampled == 1


def test_robots_que_proibe_tudo_marca_robots_disallowed():
    fetcher = FakeFetcher(
        {"https://exemplo.com/robots.txt": b"User-agent: *\nDisallow: /\nSitemap: https://x/s.xml"}
    )

    result = probe(make_site(), fetcher)

    assert result.status is ProbeStatus.ROBOTS_DISALLOWED
    assert fetcher.calls == ["https://exemplo.com/robots.txt"]


def test_respeita_orcamento_de_requisicoes_de_sitemap():
    children = tuple((f"https://exemplo.com/s{i}.xml", f"2026-10-{i:02d}") for i in range(1, 11))
    responses: dict[str, FetchResult | bytes] = {
        "https://exemplo.com/robots.txt": b"Sitemap: https://exemplo.com/index.xml\n",
        "https://exemplo.com/index.xml": index(*children),
    }
    for loc, _ in children:
        responses[loc] = urlset((loc.replace("/s", "/artigo"), "2026-10-15"))
    fetcher = FakeFetcher(responses)

    result = probe(make_site(), fetcher, max_sitemap_fetches=3)

    sitemap_calls = [c for c in fetcher.calls if c.endswith(".xml")]
    assert len(sitemap_calls) == 3
    assert result.status is ProbeStatus.OK


def test_prefere_filho_news_e_depois_o_mais_recente():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": b"Sitemap: https://exemplo.com/index.xml\n",
            "https://exemplo.com/index.xml": index(
                ("https://exemplo.com/antigo.xml", "2026-01-01"),
                ("https://exemplo.com/recente.xml", "2026-10-14"),
                ("https://exemplo.com/news-sitemap.xml", "2025-01-01"),
            ),
        }
    )

    probe(make_site(), fetcher, max_sitemap_fetches=3)

    assert fetcher.calls[2:] == [
        "https://exemplo.com/news-sitemap.xml",
        "https://exemplo.com/recente.xml",
    ]
