"""Avaliação de um endereço candidato: baixar, ler (sitemap, índice ou feed) e julgar."""

from tests.strategy.helpers import (
    HOME,
    NOW,
    UA,
    FakeFetcher,
    make_site,
    news_sitemap,
    plain_sitemap,
    rss,
    sitemap_index,
    status,
)

from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.domain.strategy import CandidateOrigin, StrategyKind, Verdict
from buscador_midias.net.fetcher import FetchResult
from buscador_midias.strategy.evaluate import evaluate_candidate
from buscador_midias.strategy.scoring import ScoringPolicy

NEWS = f"{HOME}/news-sitemap.xml"


def evaluate(
    fetcher,
    url=NEWS,
    *,
    robots=None,
    scoring=None,
    max_children=3,
    thematic_children=16,
    site=None,
    origin=CandidateOrigin.ROBOTS,
    allow_listing=False,
):
    return evaluate_candidate(
        url,
        origin,
        site=site or make_site(),
        fetcher=fetcher,
        robots=robots or RobotsPolicy.allow_all(),
        user_agent=UA,
        now=NOW,
        scoring=scoring,
        max_children=max_children,
        thematic_children=thematic_children,
        allow_listing=allow_listing,
    )


def test_sitemap_de_noticias_recente_e_fresco():
    fetcher = FakeFetcher({NEWS: news_sitemap(("a", 1), ("b", 5), ("c", 100))})

    report = evaluate(fetcher)

    assert report.verdict is Verdict.FRESH
    assert report.kind is StrategyKind.NEWS_SITEMAP
    assert report.measure is not None
    assert (report.measure.items, report.measure.recent, report.measure.titled) == (3, 2, 3)
    assert report.requests == 1 and report.score > 0 and report.role is None


def test_sitemap_comum_e_do_tipo_sitemap():
    fetcher = FakeFetcher({NEWS: plain_sitemap(("a", 1))})

    assert evaluate(fetcher).kind is StrategyKind.SITEMAP


def test_feed_e_do_tipo_feed_e_le_as_datas_do_rss():
    fetcher = FakeFetcher({NEWS: rss(("a", 1), ("b", 2), ("c", 300))})

    report = evaluate(fetcher)

    assert report.kind is StrategyKind.FEED
    assert report.measure is not None and report.measure.recent == 2


def test_indice_le_so_os_melhores_filhos_e_soma_os_itens():
    kids = {
        f"{HOME}/sitemap-news-1.xml": news_sitemap(("n1", 1), ("n2", 2)),
        f"{HOME}/post-2026-10.xml": plain_sitemap(("p1", 3)),
        f"{HOME}/post-2025-01.xml": plain_sitemap(("velho", 9000)),
        f"{HOME}/tag-sitemap.xml": plain_sitemap(("tag", 1)),
    }
    index = sitemap_index(
        (f"{HOME}/tag-sitemap.xml", 1),
        (f"{HOME}/post-2025-01.xml", 9000),
        (f"{HOME}/post-2026-10.xml", 2),
        (f"{HOME}/sitemap-news-1.xml", 1),
    )
    fetcher = FakeFetcher({NEWS: index, **kids})

    report = evaluate(fetcher, max_children=2)

    assert report.kind is StrategyKind.NEWS_SITEMAP  # algum filho é de notícias
    assert report.measure is not None and report.measure.items == 3
    assert report.requests == 3
    assert f"{HOME}/tag-sitemap.xml" not in fetcher.calls  # listagem não se lê
    assert f"{HOME}/post-2025-01.xml" not in fetcher.calls  # fora da janela de filhos


def test_indice_cujo_filho_falha_usa_o_que_leu_dos_outros():
    index = sitemap_index((f"{HOME}/a.xml", 1), (f"{HOME}/b.xml", 2))
    fetcher = FakeFetcher({NEWS: index, f"{HOME}/b.xml": plain_sitemap(("x", 1))})

    report = evaluate(fetcher)

    assert report.verdict is Verdict.FRESH
    assert report.measure is not None and report.measure.items == 1


def test_indice_em_que_nenhum_filho_le_leva_o_motivo_da_falha():
    index = sitemap_index((f"{HOME}/a.xml", 1), (f"{HOME}/b.xml", 2))
    fetcher = FakeFetcher({NEWS: index, f"{HOME}/a.xml": status(f"{HOME}/a.xml", 403)})

    report = evaluate(fetcher)

    assert report.verdict is Verdict.BLOCKED
    assert report.requests == 3 and report.score == 0


def test_indice_de_indices_nao_e_seguido():
    index = sitemap_index((f"{HOME}/outro-indice.xml", 1))
    fetcher = FakeFetcher(
        {NEWS: index, f"{HOME}/outro-indice.xml": sitemap_index((f"{HOME}/z", 1))}
    )

    report = evaluate(fetcher)

    assert report.verdict is Verdict.INVALID
    assert f"{HOME}/z" not in fetcher.calls


def test_arquivo_gigante_sem_recentes_e_arquivo_e_nao_pontua():
    scoring = ScoringPolicy(archive_min_items=50)
    fetcher = FakeFetcher({NEWS: plain_sitemap(*((f"m{n}", 9000.0) for n in range(80)))})

    report = evaluate(fetcher, scoring=scoring)

    assert report.verdict is Verdict.ARCHIVE and report.score == 0


def test_bloqueio_indisponivel_e_pagina_html_sao_distinguidos():
    assert evaluate(FakeFetcher({NEWS: status(NEWS, 403)})).verdict is Verdict.BLOCKED
    assert evaluate(FakeFetcher({NEWS: status(NEWS, 429)})).verdict is Verdict.BLOCKED
    assert evaluate(FakeFetcher({})).verdict is Verdict.UNAVAILABLE
    assert evaluate(FakeFetcher({NEWS: status(NEWS, 500)})).verdict is Verdict.UNAVAILABLE
    broken = FetchResult(NEWS, None, b"", "ConnectError: x")
    assert evaluate(FakeFetcher({NEWS: broken})).verdict is Verdict.UNAVAILABLE
    html = b"<!doctype html><html><body>Acesso negado</body></html>"
    assert evaluate(FakeFetcher({NEWS: html})).verdict is Verdict.INVALID


def test_robots_que_proibe_nao_baixa_nada():
    robots = RobotsPolicy.parse("User-agent: *\nDisallow: /news-sitemap.xml")
    fetcher = FakeFetcher({NEWS: news_sitemap(("a", 1))})

    report = evaluate(fetcher, robots=robots)

    assert report.verdict is Verdict.DISALLOWED
    assert fetcher.calls == [] and report.requests == 0


def test_urls_de_outro_dominio_nao_contam_e_sitemap_so_com_elas_e_vazio():
    foreign = news_sitemap(("a", 1), ("b", 2), host="https://outro.com")
    fetcher = FakeFetcher({NEWS: foreign})

    report = evaluate(fetcher)

    assert report.verdict is Verdict.EMPTY
    assert "nenhuma URL da fonte" in report.detail


def test_guarda_as_materias_recentes_e_as_secoes_do_candidato():
    fetcher = FakeFetcher(
        {NEWS: news_sitemap(("politica/a", 1), ("politica/b", 2), ("esporte/c", 100))}
    )

    report = evaluate(fetcher)

    assert report.recent_urls == {f"{HOME}/politica/a", f"{HOME}/politica/b"}
    assert report.measure is not None
    assert report.measure.top_sections == (("politica", 2),)


def test_secao_da_fonte_de_secao_nao_conta_o_prefixo():
    site = make_site(f"{HOME}/globonews")
    fetcher = FakeFetcher({NEWS: news_sitemap(("globonews/mundo/a", 1), ("globonews/mundo/b", 1))})

    report = evaluate(fetcher, site=site)

    assert report.measure is not None and report.measure.top_sections == (("mundo", 2),)


def test_candidato_que_nao_e_fresco_nao_guarda_materias_recentes():
    fetcher = FakeFetcher({NEWS: news_sitemap(("a", 900))})

    assert evaluate(fetcher).recent_urls == frozenset()


def test_indice_cujos_filhos_sao_feeds_vale_como_feed():
    """O índice do The Age lista dezenas de feeds por seção, não sitemaps."""
    index = sitemap_index((f"{HOME}/rss/a.xml", None), (f"{HOME}/rss/b.xml", None))
    fetcher = FakeFetcher(
        {
            NEWS: index,
            f"{HOME}/rss/a.xml": rss(("a1", 1), ("a2", 2)),
            f"{HOME}/rss/b.xml": rss(("b1", 3)),
        }
    )

    report = evaluate(fetcher)

    assert report.kind is StrategyKind.FEED and report.verdict is Verdict.FRESH
    assert report.measure is not None and report.measure.items == 3


def test_indice_sem_datas_le_um_arquivo_por_familia_ate_o_teto():
    """Regiões e temas (ANSA): nada diz qual é o mais novo, então lê-se mais que os 3 de sempre."""
    names = ["abruzzo", "lazio", "sicilia", "politica", "esteri"]
    index = sitemap_index(*((f"{HOME}/sitemaps/{n}.xml", None) for n in names))
    kids = {f"{HOME}/sitemaps/{n}.xml": plain_sitemap((f"{n}-1", 1), (f"{n}-2", 2)) for n in names}
    fetcher = FakeFetcher({NEWS: index, **kids})

    report = evaluate(fetcher, max_children=2, thematic_children=4)

    assert report.measure is not None and report.measure.items == 8
    assert report.requests == 5  # o índice e 4 filhos
    assert "4 de 5" in report.detail


def test_no_indice_sem_datas_so_o_ultimo_de_cada_familia_numerada_e_lido():
    """WordPress: posts-post-1..3 são páginas da mesma lista; a última é a mais nova."""
    index = sitemap_index(
        (f"{HOME}/wp-sitemap-posts-post-1.xml", None),
        (f"{HOME}/wp-sitemap-posts-post-2.xml", None),
        (f"{HOME}/wp-sitemap-posts-post-3.xml", None),
        (f"{HOME}/wp-sitemap-posts-noticia-1.xml", None),
    )
    fetcher = FakeFetcher(
        {
            NEWS: index,
            f"{HOME}/wp-sitemap-posts-post-3.xml": plain_sitemap(("novo", 1)),
            f"{HOME}/wp-sitemap-posts-noticia-1.xml": plain_sitemap(("noticia", 1)),
        }
    )

    evaluate(fetcher)

    assert f"{HOME}/wp-sitemap-posts-post-3.xml" in fetcher.calls
    assert f"{HOME}/wp-sitemap-posts-post-1.xml" not in fetcher.calls
    assert f"{HOME}/wp-sitemap-posts-post-2.xml" not in fetcher.calls


def test_indice_com_lastmod_todo_igual_tambem_nao_se_ordena_por_data():
    index = sitemap_index(*((f"{HOME}/s/{n}.xml", 5.0) for n in "abcd"))
    kids = {f"{HOME}/s/{n}.xml": plain_sitemap((n, 1)) for n in "abcd"}
    fetcher = FakeFetcher({NEWS: index, **kids})

    report = evaluate(fetcher, max_children=2, thematic_children=4)

    assert report.measure is not None and report.measure.items == 4


def test_indice_de_uma_familia_so_com_datas_distintas_le_so_os_mais_recentes():
    index = sitemap_index(*((f"{HOME}/s/dia-{n}.xml", float(n)) for n in range(1, 5)))
    kids = {f"{HOME}/s/dia-{n}.xml": plain_sitemap((str(n), 1)) for n in range(1, 5)}
    fetcher = FakeFetcher({NEWS: index, **kids})

    report = evaluate(fetcher, max_children=2, thematic_children=4)

    assert report.measure is not None and report.measure.items == 2


def test_sitemap_de_tags_ou_produtos_no_topo_nao_e_avaliado():
    for name in ("post_tag-sitemap.xml", "category-sitemap.xml", "sitemap_products_1.xml"):
        url = f"{HOME}/{name}"
        fetcher = FakeFetcher({url: plain_sitemap(("x", 1))})

        report = evaluate(fetcher, url)

        assert report.verdict is Verdict.INVALID and report.score == 0, name
        assert fetcher.calls == [] and report.requests == 0, name


def test_endereco_forcado_a_mao_de_listagem_e_respeitado():
    url = f"{HOME}/post_tag-sitemap.xml"
    fetcher = FakeFetcher({url: plain_sitemap(("x", 1))})

    report = evaluate(fetcher, url, allow_listing=True)

    assert report.verdict is Verdict.FRESH and fetcher.calls == [url]


def test_indice_de_categorias_por_ano_le_o_ano_mais_novo_de_cada_categoria():
    """olhardireto: /sitemap/<categoria>/<ano>.xml, do mais novo ao mais antigo."""
    locs = [f"{HOME}/sitemap/{c}/{y}.xml" for c in ("conceito", "juridico") for y in (2026, 2025)]
    index = sitemap_index((f"{HOME}/sitemap-news.xml", None), *((loc, None) for loc in locs))
    fetcher = FakeFetcher(
        {
            NEWS: index,
            f"{HOME}/sitemap-news.xml": news_sitemap(("n", 1)),
            **{loc: plain_sitemap((loc[-20:], 1)) for loc in locs},
        }
    )

    evaluate(fetcher)

    assert f"{HOME}/sitemap/conceito/2026.xml" in fetcher.calls
    assert f"{HOME}/sitemap/juridico/2026.xml" in fetcher.calls
    assert f"{HOME}/sitemap/conceito/2025.xml" not in fetcher.calls
    assert f"{HOME}/sitemap/juridico/2025.xml" not in fetcher.calls
