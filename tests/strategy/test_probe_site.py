"""Sondagem de uma fonte: gera candidatos em etapas, avalia, pontua e escolhe."""

from tests.strategy.helpers import (
    HOME,
    NOW,
    UA,
    FakeFetcher,
    home_page,
    make_site,
    news_sitemap,
    plain_sitemap,
    robots,
    rss,
    sitemap_index,
    status,
)

from buscador_midias.discovery.explore import ProbeStatus
from buscador_midias.domain.strategy import CandidateOrigin, Role, StrategyKind, Verdict
from buscador_midias.strategy.probe import probe_site
from buscador_midias.strategy.scoring import ScoringPolicy

NEWS = f"{HOME}/news-sitemap.xml"
FEED = f"{HOME}/feed/"
ROBOTS = f"{HOME}/robots.txt"


def probe(
    fetcher,
    *,
    hints=(),
    scoring=None,
    max_requests=40,
    site=None,
    exclude=(),
    forced=(),
    listing_pages=0,
    section_requests=0,
):
    return probe_site(
        site or make_site(),
        fetcher,
        user_agent=UA,
        now=NOW,
        hints=hints,
        exclude=exclude,
        forced=forced,
        listing_pages=listing_pages,
        section_requests=section_requests,
        scoring=scoring,
        max_requests=max_requests,
    )


def by_url(result):
    return {r.url: r for r in result.reports}


def test_robots_aponta_o_sitemap_de_noticias_e_a_home_aponta_o_feed():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(NEWS),
            NEWS: news_sitemap(*((f"n{i}", 1) for i in range(30))),
            HOME: home_page(FEED),
            FEED: rss(*((f"f{i}", 1) for i in range(10))),
        }
    )

    result = probe(fetcher)

    assert result.status is ProbeStatus.OK
    assert result.primary is not None and result.primary.url == NEWS
    assert result.primary.kind is StrategyKind.NEWS_SITEMAP
    assert result.backup is not None and result.backup.url == FEED
    assert by_url(result)[FEED].origin is CandidateOrigin.HOME_LINK
    assert by_url(result)[NEWS].origin is CandidateOrigin.ROBOTS


def test_dica_e_sondada_primeiro_e_nao_se_repete_se_o_robots_declara_a_mesma():
    fetcher = FakeFetcher({ROBOTS: robots(NEWS), NEWS: news_sitemap(("a", 1))})

    result = probe(fetcher, hints=(NEWS,))

    assert fetcher.calls.count(NEWS) == 1
    assert by_url(result)[NEWS].origin is CandidateOrigin.HINT


def test_dica_velha_que_nao_serve_nao_atrapalha():
    fetcher = FakeFetcher(
        {ROBOTS: robots(NEWS), NEWS: news_sitemap(("a", 1)), f"{HOME}/velha.xml": status("", 404)}
    )

    result = probe(fetcher, hints=(f"{HOME}/velha.xml",))

    assert by_url(result)[f"{HOME}/velha.xml"].verdict is Verdict.UNAVAILABLE
    assert result.primary is not None and result.primary.url == NEWS


def test_sem_sitemap_de_noticias_declarado_tenta_os_caminhos_comuns():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(f"{HOME}/sitemap.xml"),
            f"{HOME}/sitemap.xml": plain_sitemap(("a", 1)),
            f"{HOME}/sitemap-news.xml": news_sitemap(*((f"n{i}", 1) for i in range(20))),
        }
    )

    result = probe(fetcher)

    assert result.primary is not None and result.primary.url == f"{HOME}/sitemap-news.xml"
    assert by_url(result)[f"{HOME}/sitemap-news.xml"].origin is CandidateOrigin.COMMON_PATH


def test_com_sitemap_de_noticias_valido_nao_adivinha_caminhos():
    fetcher = FakeFetcher({ROBOTS: robots(NEWS), NEWS: news_sitemap(("a", 1))})

    probe(fetcher)

    assert f"{HOME}/sitemap-news.xml" not in fetcher.calls
    assert f"{HOME}/news_sitemap.xml" not in fetcher.calls


def test_robots_sem_sitemap_declarado_tenta_sitemap_xml_e_sitemap_index_xml():
    fetcher = FakeFetcher({ROBOTS: robots(), f"{HOME}/sitemap.xml": plain_sitemap(("a", 1))})

    probe(fetcher)

    assert f"{HOME}/sitemap.xml" in fetcher.calls and f"{HOME}/sitemap_index.xml" in fetcher.calls


def test_sem_feed_declarado_adivinha_e_para_no_primeiro_que_serve():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(NEWS),
            NEWS: news_sitemap(("a", 1)),
            HOME: home_page(),
            f"{HOME}/rss.xml": rss(("x", 1)),
        }
    )

    result = probe(fetcher)

    assert by_url(result)[f"{HOME}/rss.xml"].verdict is Verdict.FRESH
    assert f"{HOME}/rss" not in fetcher.calls


def test_feed_ja_achado_por_dica_dispensa_buscar_outro():
    fetcher = FakeFetcher({ROBOTS: robots(NEWS), NEWS: news_sitemap(("a", 1)), FEED: rss(("x", 1))})

    probe(fetcher, hints=(FEED,))

    assert HOME not in fetcher.calls  # a home só se baixa para achar um feed que ainda não há


def test_caso_arquivo_o_indice_enorme_e_descartado_e_o_sitemap_de_noticias_vence():
    archive_index = f"{HOME}/sitemap-archive.xml"
    year = f"{HOME}/archive-2001.xml"
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(archive_index, NEWS),
            archive_index: sitemap_index((year, 9000)),
            year: plain_sitemap(*((f"velho{n}", 9000.0) for n in range(60))),
            NEWS: news_sitemap(*((f"n{i}", 2) for i in range(10))),
        }
    )

    result = probe(fetcher, scoring=ScoringPolicy(archive_min_items=50))

    assert by_url(result)[archive_index].verdict is Verdict.ARCHIVE
    assert result.primary is not None and result.primary.url == NEWS


def test_robots_que_bloqueia_encerra_a_sondagem_com_o_motivo():
    fetcher = FakeFetcher({ROBOTS: status(ROBOTS, 403)})

    result = probe(fetcher)

    assert result.status is ProbeStatus.BLOCKED
    assert result.reports == () and result.primary is None
    assert result.requests == 1


def test_robots_que_proibe_a_raiz_encerra_a_sondagem():
    result = probe(FakeFetcher({ROBOTS: robots(disallow_all=True)}))

    assert result.status is ProbeStatus.ROBOTS_DISALLOWED


def test_fonte_sem_nada_que_sirva_termina_sem_escolha_e_com_status_de_falha():
    fetcher = FakeFetcher({ROBOTS: robots(NEWS), NEWS: status(NEWS, 403)})

    result = probe(fetcher)

    assert result.primary is None and result.backup is None
    assert result.status is ProbeStatus.BLOCKED
    assert by_url(result)[NEWS].verdict is Verdict.BLOCKED


def test_fonte_sem_candidato_legivel_e_no_sitemap():
    result = probe(FakeFetcher({ROBOTS: robots()}))

    assert result.status is ProbeStatus.NO_SITEMAP


def test_o_orcamento_de_requisicoes_e_respeitado():
    fetcher = FakeFetcher({ROBOTS: robots()})

    result = probe(fetcher, max_requests=4)

    assert result.requests <= 4
    assert len(fetcher.calls) <= 4


def test_so_o_principal_e_a_reserva_recebem_papel():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(NEWS, f"{HOME}/sitemap.xml"),
            NEWS: news_sitemap(*((f"n{i}", 1) for i in range(30))),
            f"{HOME}/sitemap.xml": plain_sitemap(("a", 1)),
            HOME: home_page(FEED),
            FEED: rss(("x", 1)),
        }
    )

    result = probe(fetcher)

    roles = {r.url: r.role for r in result.reports}
    assert roles[NEWS] is Role.PRIMARY and roles[FEED] is Role.BACKUP
    assert roles[f"{HOME}/sitemap.xml"] is None


def test_dica_com_data_velha_vira_modelo_e_sonda_o_endereco_de_hoje():
    template = f"{HOME}/sitemap/{{YYYY}}-{{MM}}-{{DD}}/news.xml"
    today = f"{HOME}/sitemap/2026-10-16/news.xml"
    fetcher = FakeFetcher(
        {ROBOTS: robots(), today: news_sitemap(*((f"n{i}", 1) for i in range(5)))}
    )

    result = probe(fetcher, hints=(f"{HOME}/sitemap/2026-10-07/news.xml",))

    assert today in fetcher.calls
    assert result.primary is not None and result.primary.url == template


def test_sitemap_com_data_declarado_no_robots_tambem_vira_modelo():
    template = f"{HOME}/s-{{YYYY}}-{{MM}}.xml"
    fetcher = FakeFetcher(
        {ROBOTS: robots(f"{HOME}/s-2026-09.xml"), f"{HOME}/s-2026-10.xml": news_sitemap(("a", 1))}
    )

    result = probe(fetcher)

    assert result.primary is not None and result.primary.url == template


def test_sitemap_de_arquivo_com_ano_antigo_nao_vira_modelo():
    old = f"{HOME}/post-sitemap-2019-04.xml"
    fetcher = FakeFetcher({ROBOTS: robots(old), old: plain_sitemap(("a", 1))})

    result = probe(fetcher)

    assert old in {r.url for r in result.reports}


def test_complemento_acrescenta_o_que_o_principal_nao_lista_e_mede_a_cobertura():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(NEWS, f"{HOME}/sitemap.xml"),
            NEWS: news_sitemap(*((f"politica/n{i}", 1) for i in range(30))),
            f"{HOME}/sitemap.xml": plain_sitemap(*((f"esporte/s{i}", 1) for i in range(20))),
            HOME: home_page(FEED),
            FEED: rss(*((f"mundo/f{i}", 1) for i in range(10))),
        }
    )

    result = probe(fetcher)

    roles = {r.url: r.role for r in result.reports}
    assert roles[NEWS] is Role.PRIMARY
    assert roles[f"{HOME}/sitemap.xml"] is Role.BACKUP
    assert roles[FEED] is Role.COMPLEMENT
    assert result.primary is not None
    assert result.primary.union_recent == 60
    assert result.primary.coverage == 30 / 60
    assert result.primary.cover_share == 1.0
    assert result.complements == (by_url(result)[FEED],)


def test_fonte_com_um_so_candidato_cobre_tudo_que_viu():
    fetcher = FakeFetcher({ROBOTS: robots(NEWS), NEWS: news_sitemap(("a", 1), ("b", 1))})

    result = probe(fetcher)

    assert result.primary is not None and result.primary.cover_share == 1.0
    assert result.complements == ()


class FlakyFetcher(FakeFetcher):
    """Falha com 404 as primeiras vezes que uma URL é pedida (CDN instável)."""

    def __init__(self, responses, flaky: dict[str, int]) -> None:
        super().__init__(responses)
        self.flaky = dict(flaky)

    def get(self, url):
        if self.flaky.get(url, 0) > 0:
            self.flaky[url] -= 1
            self.calls.append(url)
            return status(url, 404)
        return super().get(url)


def test_sitemap_declarado_no_robots_que_falha_uma_vez_ganha_outra_tentativa():
    fetcher = FlakyFetcher(
        {ROBOTS: robots(NEWS), NEWS: news_sitemap(*((f"n{i}", 1) for i in range(8)))},
        flaky={NEWS: 1},
    )

    result = probe(fetcher)

    assert result.primary is not None and result.primary.url == NEWS
    assert fetcher.calls.count(NEWS) == 2


def test_endereco_adivinhado_que_falha_nao_e_repetido():
    fetcher = FlakyFetcher(
        {ROBOTS: robots(), NEWS: news_sitemap(*((f"n{i}", 1) for i in range(8)))},
        flaky={NEWS: 1},
    )

    probe(fetcher)

    assert fetcher.calls.count(NEWS) == 1


def test_sitemap_so_de_tags_declarado_no_robots_cai_nos_caminhos_comuns():
    tags = f"{HOME}/post_tag-sitemap.xml"
    fetcher = FakeFetcher(
        {ROBOTS: robots(tags), f"{HOME}/sitemap.xml": plain_sitemap(("a", 1), ("b", 2))}
    )

    result = probe(fetcher)

    assert tags not in fetcher.calls
    assert result.primary is not None and result.primary.url == f"{HOME}/sitemap.xml"


def test_endereco_excluido_a_mao_nao_e_baixado_e_aparece_como_excluido():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(NEWS),
            NEWS: news_sitemap(*((f"n{i}", 1) for i in range(8))),
            f"{HOME}/sitemap.xml": plain_sitemap(("a", 1), ("b", 2)),
        }
    )

    result = probe(fetcher, exclude=[NEWS])

    assert NEWS not in fetcher.calls
    excluded = by_url(result)[NEWS]
    assert excluded.verdict is Verdict.INVALID and "excluíd" in excluded.detail


def test_exclusao_com_asterisco_vale_para_o_prefixo():
    fetcher = FakeFetcher(
        {ROBOTS: robots(f"{HOME}/drtv/a.xml"), f"{HOME}/drtv/a.xml": plain_sitemap(("a", 1))}
    )

    probe(fetcher, exclude=[f"{HOME}/drtv/*"])

    assert f"{HOME}/drtv/a.xml" not in fetcher.calls


def test_endereco_forcado_a_mao_vira_o_principal_mesmo_com_nota_menor():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(NEWS),
            NEWS: news_sitemap(*((f"n{i}", 1) for i in range(30))),
            FEED: rss(("f1", 1), ("f2", 2)),
        }
    )

    result = probe(fetcher, forced=[FEED])

    assert result.primary is not None and result.primary.url == FEED
    assert result.backup is not None and result.backup.url == NEWS


def test_dica_do_levantamento_que_e_listagem_de_tags_nao_e_baixada():
    tags = f"{HOME}/post_tag-sitemap.xml"
    fetcher = FakeFetcher({ROBOTS: robots(), tags: plain_sitemap(("x", 1))})

    probe(fetcher, hints=[tags])

    assert tags not in fetcher.calls


def test_endereco_forcado_de_listagem_e_baixado():
    tags = f"{HOME}/post_tag-sitemap.xml"
    fetcher = FakeFetcher(
        {ROBOTS: robots(), tags: plain_sitemap(*((f"t{i}", 1) for i in range(6)))}
    )

    result = probe(fetcher, forced=[tags])

    assert result.primary is not None and result.primary.url == tags


def test_feed_lento_vira_estrategia_quando_nao_ha_nada_melhor():
    fetcher = FakeFetcher({ROBOTS: robots(), FEED: rss(("a", 240), ("b", 300))})

    result = probe(fetcher)

    assert result.status is ProbeStatus.OK
    assert result.primary is not None and result.primary.verdict is Verdict.SLOW


def test_sitemap_lento_declarado_nao_dispensa_procurar_o_de_noticias():
    slow = f"{HOME}/sitemap.xml"
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(slow),
            slow: plain_sitemap(("velho", 240.0)),
            NEWS: news_sitemap(*((f"n{i}", 1) for i in range(8))),
        }
    )

    result = probe(fetcher)

    assert NEWS in fetcher.calls
    assert result.primary is not None and result.primary.url == NEWS


def listing_home() -> bytes:
    links = "".join(
        f'<a href="/economia/governo-anuncia-medida-numero-{n}-para-o-setor">x</a>'
        for n in range(10)
    )
    return f'<html><body><a href="/economia">Economia</a>{links}</body></html>'.encode()


def test_sem_sitemap_nem_feed_a_varredura_da_home_vira_a_estrategia():
    fetcher = FakeFetcher({ROBOTS: robots(), HOME: listing_home()})

    result = probe(fetcher, listing_pages=10)

    assert result.status is ProbeStatus.OK
    assert result.primary is not None
    assert result.primary.kind is StrategyKind.LISTING and result.primary.url == HOME


def test_a_varredura_roda_primeiro_mesmo_com_feed_e_vira_a_reserva_de_outro_tipo():
    """A varredura mapeia a estrutura do site; o feed continua sendo o que se lê."""
    fetcher = FakeFetcher({ROBOTS: robots(), HOME: listing_home(), FEED: rss(("a", 1), ("b", 2))})

    result = probe(fetcher, listing_pages=10)

    assert result.primary is not None and result.primary.kind is StrategyKind.FEED
    assert result.backup is not None and result.backup.kind is StrategyKind.LISTING
    assert fetcher.calls.index(f"{HOME}/economia") < fetcher.calls.index(FEED)  # primeiro


def test_fonte_lenta_continua_principal_e_a_varredura_fica_de_reserva():
    fetcher = FakeFetcher({ROBOTS: robots(), HOME: listing_home(), FEED: rss(("a", 240))})

    result = probe(fetcher, listing_pages=10)

    assert result.primary is not None and result.primary.verdict is Verdict.SLOW
    assert result.backup is not None and result.backup.kind is StrategyKind.LISTING


def test_o_teto_de_paginas_da_varredura_e_proprio_e_nao_gasta_o_orcamento_dos_candidatos():
    fetcher = FakeFetcher(
        {ROBOTS: robots(NEWS), NEWS: news_sitemap(("n1", 1)), HOME: listing_home()}
    )

    result = probe(fetcher, max_requests=2, listing_pages=10)

    assert NEWS in fetcher.calls  # o sitemap foi lido apesar da varredura
    listing = next(r for r in result.reports if r.kind is StrategyKind.LISTING)
    assert listing.requests >= 2


def test_varredura_desligada_com_zero_paginas():
    fetcher = FakeFetcher({ROBOTS: robots(), HOME: listing_home()})

    result = probe(fetcher, listing_pages=0)

    assert result.primary is None
    assert all(r.kind is not StrategyKind.LISTING for r in result.reports)


def test_home_sem_materias_leva_o_motivo_da_falha_com_a_varredura_contada():
    fetcher = FakeFetcher({ROBOTS: robots(), HOME: b"<html><body>nada</body></html>"})

    result = probe(fetcher, listing_pages=10)

    assert result.status is ProbeStatus.NO_SITEMAP and "empty" in result.detail


def test_endereco_da_home_excluido_a_mao_dispensa_a_varredura():
    fetcher = FakeFetcher({ROBOTS: robots(), HOME: listing_home()})

    result = probe(fetcher, exclude=[HOME], listing_pages=10)

    assert result.primary is None


# --- fontes por seção ---


def section_home(*extra: str) -> bytes:
    sections = '<a href="/economia">E</a><a href="/politica">P</a><a href="/mundo">M</a>'
    return f"<html><body>{sections}{''.join(extra)}</body></html>".encode()


def section_feed(section: str, n: int = 6) -> bytes:
    return rss(*((f"{section}/materia-{i}", i) for i in range(n)))


def probe_sections(fetcher, **kw):
    return probe(fetcher, listing_pages=10, section_requests=80, **kw)


def test_feed_declarado_na_pagina_de_secao_vira_candidato():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(),
            HOME: section_home(),
            f"{HOME}/economia": (
                b'<html><head><link rel="alternate" type="application/rss+xml" '
                b'href="/feeds/economia.xml"></head></html>'
            ),
            f"{HOME}/feeds/economia.xml": section_feed("economia"),
        }
    )

    result = probe_sections(fetcher)

    feed = by_url(result)[f"{HOME}/feeds/economia.xml"]
    assert feed.verdict is Verdict.FRESH and feed.role is not None
    assert feed.origin is CandidateOrigin.HOME_LINK


def test_pagina_indice_de_feeds_e_lida_e_os_feeds_dela_sao_medidos():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(),
            HOME: section_home('<a href="/lista-de-feeds.shtml">feeds</a>'),
            f"{HOME}/lista-de-feeds.shtml": (
                b'<html><a href="/rss/politica.xml">p</a><a href="/rss/mundo.xml">m</a></html>'
            ),
            f"{HOME}/rss/politica.xml": section_feed("politica"),
            f"{HOME}/rss/mundo.xml": section_feed("mundo"),
        }
    )

    result = probe_sections(fetcher)

    reports = by_url(result)
    assert reports[f"{HOME}/rss/politica.xml"].verdict is Verdict.FRESH
    assert reports[f"{HOME}/rss/mundo.xml"].verdict is Verdict.FRESH
    roles = {r.url: r.role for r in result.reports if r.role}
    assert Role.COMPLEMENT in roles.values() or Role.BACKUP in roles.values()


def test_modelo_de_url_de_uma_secao_e_testado_nas_outras():
    """/rss/economia/ serve; economia é seção do site; então /rss/politica/ e /rss/mundo/ também."""
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(),
            HOME: section_home('<a href="/rss/economia/">rss</a>'),
            f"{HOME}/rss/economia/": section_feed("economia"),
            f"{HOME}/rss/politica/": section_feed("politica"),
            f"{HOME}/rss/mundo/": section_feed("mundo"),
        }
    )

    result = probe_sections(fetcher)

    assert f"{HOME}/rss/politica/" in fetcher.calls and f"{HOME}/rss/mundo/" in fetcher.calls
    covered = {r.url for r in result.reports if r.role}
    assert {f"{HOME}/rss/economia/", f"{HOME}/rss/politica/", f"{HOME}/rss/mundo/"} <= covered


def test_url_gerada_pelo_modelo_que_nao_existe_nao_fica_nos_candidatos():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(),
            HOME: section_home('<a href="/rss/economia/">rss</a>'),
            f"{HOME}/rss/economia/": section_feed("economia"),
        }
    )

    result = probe_sections(fetcher)

    assert f"{HOME}/rss/politica/" in fetcher.calls  # foi testada...
    assert f"{HOME}/rss/politica/" not in by_url(result)  # ...e o 404 não polui a lista


def test_o_orcamento_das_fontes_por_secao_e_proprio_e_limita_as_requisicoes():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(),
            HOME: section_home('<a href="/rss/economia/">rss</a>'),
            f"{HOME}/rss/economia/": section_feed("economia"),
            f"{HOME}/rss/politica/": section_feed("politica"),
            f"{HOME}/rss/mundo/": section_feed("mundo"),
        }
    )

    probe(fetcher, listing_pages=10, section_requests=1)

    assert f"{HOME}/rss/economia/" in fetcher.calls
    assert f"{HOME}/rss/politica/" not in fetcher.calls


def test_sem_orcamento_nao_ha_busca_de_fontes_por_secao():
    fetcher = FakeFetcher(
        {
            ROBOTS: robots(),
            HOME: section_home('<a href="/rss/economia/">rss</a>'),
            f"{HOME}/rss/economia/": section_feed("economia"),
        }
    )

    probe(fetcher, listing_pages=10, section_requests=0)

    assert f"{HOME}/rss/economia/" not in fetcher.calls
