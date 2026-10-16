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


def probe(fetcher, *, hints=(), scoring=None, max_requests=40, site=None):
    return probe_site(
        site or make_site(),
        fetcher,
        user_agent=UA,
        now=NOW,
        hints=hints,
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
