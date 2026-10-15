from datetime import UTC, datetime

from buscador_midias.discovery.explore import ProbeStatus, explore_site
from buscador_midias.domain.site import Site
from buscador_midias.domain.sitemap import SitemapRef
from buscador_midias.net.fetcher import FetchResult

UA = "BuscadorMidiasBot"
ROBOTS = b"Sitemap: https://exemplo.com/index.xml\n"


class FakeFetcher:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        content = self.responses.get(url)
        if content is None:
            return FetchResult(url=url, status=404, content=b"", error=None)
        return FetchResult(url=url, status=200, content=content, error=None)


def make_site() -> Site:
    return Site.create(
        name="Exemplo",
        url="https://exemplo.com",
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


def index(*children: tuple[str, str | None]) -> bytes:
    items = "".join(
        f"<sitemap><loc>{loc}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</sitemap>"
        for loc, mod in children
    )
    return (
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>'
    ).encode()


def urlset(*locs: str) -> bytes:
    urls = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode()


def responses() -> dict[str, bytes]:
    return {
        "https://exemplo.com/robots.txt": ROBOTS,
        "https://exemplo.com/index.xml": index(
            ("https://exemplo.com/velho.xml", "2026-01-01T00:00:00Z"),
            ("https://exemplo.com/novo.xml", "2026-10-15T00:00:00Z"),
        ),
        "https://exemplo.com/velho.xml": urlset("https://exemplo.com/a"),
        "https://exemplo.com/novo.xml": urlset("https://exemplo.com/b"),
    }


def explore(fetcher: FakeFetcher, **kwargs):
    return explore_site(make_site(), fetcher, user_agent=UA, max_sitemap_fetches=6, **kwargs)


def test_sem_gancho_le_todos_os_filhos_e_registra_o_lastmod_declarado():
    fetcher = FakeFetcher(responses())

    result = explore(fetcher)

    assert result.status is ProbeStatus.OK
    assert result.fetched == 3
    assert result.skipped == 0
    by_url = {e.url: e for e in result.sitemaps}
    assert by_url["https://exemplo.com/index.xml"].is_entry is True
    assert by_url["https://exemplo.com/index.xml"].declared_lastmod is None
    assert by_url["https://exemplo.com/novo.xml"].is_entry is False
    assert by_url["https://exemplo.com/novo.xml"].declared_lastmod == datetime(
        2026, 10, 15, tzinfo=UTC
    )


def test_gancho_pula_filho_inalterado_sem_fazer_requisicao():
    fetcher = FakeFetcher(responses())

    result = explore(fetcher, skip_child=lambda ref: ref.loc.endswith("velho.xml"))

    assert "https://exemplo.com/velho.xml" not in fetcher.calls
    assert result.skipped == 1
    assert result.fetched == 2


def test_tudo_pulado_e_ok_e_nao_falha():
    fetcher = FakeFetcher(responses())

    result = explore(fetcher, skip_child=lambda ref: True)

    assert result.status is ProbeStatus.OK
    assert result.skipped == 2
    assert [e.url for e in result.sitemaps] == ["https://exemplo.com/index.xml"]


def test_gancho_recebe_o_ref_com_loc_e_lastmod():
    seen: list[SitemapRef] = []
    fetcher = FakeFetcher(responses())

    explore(fetcher, skip_child=lambda ref: seen.append(ref) or False)

    assert {r.loc for r in seen} == {
        "https://exemplo.com/velho.xml",
        "https://exemplo.com/novo.xml",
    }
    assert all(r.lastmod is not None for r in seen)


def test_pular_consome_a_janela_e_nao_avanca_para_filhos_mais_antigos():
    children = tuple(
        (f"https://exemplo.com/c{n}.xml", f"2026-10-{15 - n:02d}T00:00:00Z") for n in range(1, 5)
    )
    pages = {
        "https://exemplo.com/robots.txt": ROBOTS,
        "https://exemplo.com/index.xml": index(*children),
    }
    pages.update({url: urlset(f"https://exemplo.com/p{n}") for n, (url, _) in enumerate(children)})
    fetcher = FakeFetcher(pages)

    result = explore_site(
        make_site(),
        fetcher,
        user_agent=UA,
        max_sitemap_fetches=3,  # o índice + os 2 filhos mais recentes
        skip_child=lambda ref: ref.loc.endswith("c1.xml"),
    )

    assert "https://exemplo.com/c1.xml" not in fetcher.calls
    assert "https://exemplo.com/c2.xml" in fetcher.calls
    assert "https://exemplo.com/c3.xml" not in fetcher.calls
    assert "https://exemplo.com/c4.xml" not in fetcher.calls
    assert result.fetched == 2
    assert result.skipped == 1


def test_exploracao_ok_conta_os_sitemaps_que_falharam():
    pages = responses()
    del pages["https://exemplo.com/velho.xml"]  # o filho some: o fake devolve 404
    fetcher = FakeFetcher(pages)

    result = explore(fetcher)

    assert result.status is ProbeStatus.OK
    assert result.failed == 1
    assert "velho.xml" in result.failure_detail


def test_exploracao_sem_falhas_tem_contador_zero():
    result = explore(FakeFetcher(responses()))

    assert result.failed == 0
    assert result.failure_detail == ""


def _index_pages(children: list[tuple[str, str]]) -> dict[str, bytes]:
    pages = {
        "https://exemplo.com/robots.txt": ROBOTS,
        "https://exemplo.com/index.xml": index(*children),
    }
    pages.update({loc: urlset(f"https://exemplo.com/p/{n}") for n, (loc, _) in enumerate(children)})
    return pages


def test_sitemaps_de_taxonomia_nao_sao_lidos_quando_ha_sitemap_de_materias():
    children = [
        ("https://exemplo.com/post_tag-sitemap.xml", "2026-10-15T09:00:00Z"),
        ("https://exemplo.com/author-sitemap.xml", "2026-10-15T08:00:00Z"),
        ("https://exemplo.com/category-sitemap.xml", "2026-10-15T07:00:00Z"),
        ("https://exemplo.com/post-sitemap2.xml", "2026-10-14T00:00:00Z"),
    ]
    fetcher = FakeFetcher(_index_pages(children))

    result = explore(fetcher)

    assert fetcher.calls[-1] == "https://exemplo.com/post-sitemap2.xml"
    assert len(fetcher.calls) == 3  # robots, índice e o único sitemap de matérias
    assert result.fetched == 2


def test_taxonomia_nao_toma_o_lugar_das_materias_no_orcamento():
    children = [
        ("https://exemplo.com/keyword-1.xml", "2026-10-15T12:00:00Z"),
        ("https://exemplo.com/keyword-2.xml", "2026-10-15T11:00:00Z"),
        ("https://exemplo.com/article-1.xml", "2026-10-10T00:00:00Z"),
    ]
    fetcher = FakeFetcher(_index_pages(children))

    explore_site(make_site(), fetcher, user_agent=UA, max_sitemap_fetches=2)

    assert "https://exemplo.com/article-1.xml" in fetcher.calls
    assert not any("keyword" in call for call in fetcher.calls)


def test_se_todos_os_filhos_sao_de_taxonomia_eles_ainda_sao_lidos():
    children = [
        ("https://exemplo.com/tags-1.xml", "2026-10-15T12:00:00Z"),
        ("https://exemplo.com/tags-2.xml", "2026-10-14T12:00:00Z"),
    ]
    fetcher = FakeFetcher(_index_pages(children))

    result = explore(fetcher)

    assert result.status is ProbeStatus.OK
    assert result.fetched == 3


def test_deteccao_de_taxonomia_pelo_nome_do_arquivo():
    from buscador_midias.discovery.explore import is_taxonomy_sitemap

    taxonomy = [
        "https://a.com/post_tag-sitemap3.xml",
        "https://a.com/wp-sitemap-taxonomies-post_tag-2.xml",
        "https://a.com/wp-sitemap-users-1.xml",
        "https://a.com/sitemaps/1/author-1.xml",
        "https://a.com/sitemaps/1/keyword-1.xml",
        "https://a.com/page-sitemap.xml",
        "https://a.com/sitemap/tags-6.xml",
        "https://a.com/sitemap/post_categories-1.xml",
        "https://a.com/person-sitemap.xml",
    ]
    content = [
        "https://a.com/post-sitemap14.xml",
        "https://a.com/sitemaps/1/article-news-1.xml",
        "https://a.com/sitemap-news.xml",
        "https://a.com/sitemap/ge/2026/10/15_1.xml",
        "https://a.com/wp-sitemap-posts-post-1.xml",
        "https://a.com/sitemap/latest-1.xml",
        "https://tags.example.com/story-1.xml",  # só o caminho conta, não o host
    ]
    assert all(is_taxonomy_sitemap(loc) for loc in taxonomy)
    assert not any(is_taxonomy_sitemap(loc) for loc in content)


def test_sitemaps_declarados_no_robots_vem_antes_dos_filhos_do_indice():
    robots = (
        b"Sitemap: https://exemplo.com/index.xml\n"
        b"Sitemap: https://exemplo.com/news.xml\n"
        b"Sitemap: https://exemplo.com/video.xml\n"
    )
    pages = {
        "https://exemplo.com/robots.txt": robots,
        "https://exemplo.com/index.xml": index(
            ("https://exemplo.com/c1.xml", "2026-10-15T00:00:00Z"),
            ("https://exemplo.com/c2.xml", "2026-10-14T00:00:00Z"),
        ),
        "https://exemplo.com/news.xml": urlset("https://exemplo.com/n-1"),
        "https://exemplo.com/video.xml": urlset("https://exemplo.com/v-1"),
        "https://exemplo.com/c1.xml": urlset("https://exemplo.com/p-1"),
        "https://exemplo.com/c2.xml": urlset("https://exemplo.com/p-2"),
    }
    fetcher = FakeFetcher(pages)

    result = explore_site(make_site(), fetcher, user_agent=UA, max_sitemap_fetches=4)

    assert "https://exemplo.com/news.xml" in fetcher.calls
    assert "https://exemplo.com/video.xml" in fetcher.calls
    assert "https://exemplo.com/c1.xml" in fetcher.calls  # sobrou 1 vaga para o filho mais novo
    assert "https://exemplo.com/c2.xml" not in fetcher.calls
    assert result.fetched == 4


def test_filhos_so_ocupam_o_orcamento_que_sobra_depois_dos_declarados():
    robots = b"Sitemap: https://exemplo.com/index.xml\nSitemap: https://exemplo.com/news.xml\n"
    pages = {
        "https://exemplo.com/robots.txt": robots,
        "https://exemplo.com/index.xml": index(("https://exemplo.com/c1.xml", None)),
        "https://exemplo.com/news.xml": urlset("https://exemplo.com/n-1"),
        "https://exemplo.com/c1.xml": urlset("https://exemplo.com/p-1"),
    }
    fetcher = FakeFetcher(pages)

    explore_site(make_site(), fetcher, user_agent=UA, max_sitemap_fetches=2)

    assert fetcher.calls[1:] == ["https://exemplo.com/index.xml", "https://exemplo.com/news.xml"]
