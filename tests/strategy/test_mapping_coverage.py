from tests.strategy.helpers import HOME, NOW, UA, FakeFetcher, make_site, plain_sitemap, robots

from buscador_midias.domain.mapping import MapPage, MapSource, PageStatus
from buscador_midias.strategy.mapping_coverage import (
    audit_sources,
    compare_page,
    normalize_source_audits,
    plan_source_audit,
)


def test_valida_uma_fonte_uma_vez_e_compara_amostras_por_secao():
    url = HOME + "/sitemap.xml"
    fetcher = FakeFetcher(
        {
            HOME + "/robots.txt": robots(),
            url: plain_sitemap(
                ("economia/materia-numero-um-do-jornal", 1),
                ("politica/materia-numero-dois-do-jornal", 2),
            ),
        }
    )
    checks = audit_sources(make_site(), fetcher, [url, url], user_agent=UA, now=NOW)
    assert len(checks) == 1
    page = MapPage(
        HOME + "/economia",
        HOME,
        "section",
        PageStatus.OK,
        article_urls=(
            HOME + "/economia/materia-numero-um-do-jornal",
            HOME + "/economia/materia-que-falta-no-sitemap",
        ),
        checked_at=NOW,
    )
    coverage = compare_page(page, checks)
    assert coverage.status == "partial"
    assert coverage.matched == 1 and coverage.sampled == 2
    assert coverage.sources == (url,)
    assert fetcher.calls.count(url) == 1


def test_sem_amostra_nao_vira_cobertura_total():
    page = MapPage(HOME, "", "root", PageStatus.OK, checked_at=NOW)
    assert compare_page(page, []).status == "no_sample"
    assert compare_page(MapPage(HOME, "", "root"), []).status == "not_read"


def test_html_listing_pagination_does_not_expand_into_historical_archives():
    first = HOME + "/politica/index/feed/pagina-2"
    second = HOME + "/politica/index/feed/pagina-3"
    fetcher = FakeFetcher({
        HOME + "/robots.txt": robots(),
        first: f'<html><a href="{second}">Próxima página</a></html>'.encode(),
    })
    checks = audit_sources(make_site(), fetcher, [first], user_agent=UA, now=NOW)
    assert len(checks) == 1 and checks[0].verdict == "invalid"
    assert checks[0].kind == "html_pagination" and not checks[0].listed_urls
    assert second not in fetcher.calls
    legacy = MapSource(first, "feed_index", "feed_index", (second,), NOW)
    corrected = normalize_source_audits([legacy])[0]
    assert corrected.kind == "html_pagination" and corrected.verdict == "invalid"
    assert corrected.listed_urls == legacy.listed_urls and corrected.checked_at == NOW
    assert normalize_source_audits([corrected]) == [corrected]
    ghtml = MapSource(first + ".ghtml", "feed_index", "feed_index", (second + ".ghtml",), NOW)
    assert normalize_source_audits([ghtml])[0].kind == "html_pagination"
    page = MapPage(HOME, "", "root", PageStatus.OK, feeds=(first,), checked_at=NOW)
    assert plan_source_audit(
        make_site(), [page], [legacy], [], now=NOW, retry_failed=True
    ) == []


def test_real_xml_feed_with_pagination_name_is_still_audited():
    url = HOME + "/politica/index/feed/pagina-2"
    fetcher = FakeFetcher({
        HOME + "/robots.txt": robots(),
        url: plain_sitemap(("politica/materia-com-titulo-do-jornal", 1)),
    })
    check = audit_sources(make_site(), fetcher, [url], user_agent=UA, now=NOW)[0]
    assert check.kind == "sitemap" and check.verdict != "invalid"


def test_comentarios_nao_comprovam_cobertura_apos_remover_fragmentos():
    article = HOME + "/politica/materia-com-titulo-do-jornal/"
    comments = MapSource(
        article + "feed/", "fresh", "feed", (article + "#comment-13259",), NOW
    )
    corrected = normalize_source_audits([comments])[0]
    assert corrected.kind == "comments_feed" and corrected.verdict == "invalid"
    assert corrected.listed_urls == comments.listed_urls
    page = MapPage(HOME, "", "root", PageStatus.OK, article_urls=(article,), checked_at=NOW)
    assert compare_page(page, [comments]).matched == 0
    mixed = MapSource(
        HOME + "/feed", "fresh", "feed", (article + "#comment-13259", HOME + "/outra"), NOW
    )
    assert compare_page(page, [mixed]).matched == 0


def test_complementos_acrescentam_links_e_omitem_mecanismos_redundantes():
    a, b, c, d = (HOME + path for path in ("/a", "/b", "/c", "/d"))
    page = MapPage(HOME, "", "root", PageStatus.OK, article_urls=(a, b, c, d), checked_at=NOW)
    checks = [
        MapSource(HOME + "/principal.xml", "fresh", "sitemap", (a, b), NOW),
        MapSource(HOME + "/duplicado.xml", "fresh", "feed", (a,), NOW),
        MapSource(HOME + "/complemento.xml", "undated", "feed", (b, c), NOW, partial=True),
    ]
    coverage = compare_page(page, checks)
    assert coverage.status == "partial"
    assert coverage.missing == (d,)
    assert [(c.source.url, c.matched, c.added) for c in coverage.suggested] == [
        (HOME + "/principal.xml", 2, 2),
        (HOME + "/complemento.xml", 2, 1),
    ]
    assert coverage.suggested[1].source.partial
    assert len(coverage.sources) == 3  # mantém todas as correspondências para auditoria


def test_complementos_usam_so_leituras_atuais_e_reutilizam_indice_por_secao():
    from datetime import timedelta

    from buscador_midias.strategy.mapping_coverage import CoverageIndex

    article, other = HOME + "/a", HOME + "/b"
    checks = [
        MapSource(
            HOME + "/antigo.xml", "fresh", "feed", (article, other), NOW - timedelta(hours=1)
        ),
        MapSource(HOME + "/atual.xml", "fresh", "feed", (article,), NOW),
        MapSource(HOME + "/erro.xml", "error", "feed", (other,), NOW),
        MapSource(HOME + "/indice.html", "feed_index", "feed_index", (other,), NOW),
    ]
    index = CoverageIndex(checks)
    page = MapPage(HOME, "", "root", PageStatus.OK, article_urls=(article, other), checked_at=NOW)
    coverage = index.compare(page)
    assert coverage.status == "partial"
    assert [c.source.url for c in coverage.suggested] == [HOME + "/atual.xml"]
    assert (
        index.compare(
            MapPage(
                HOME + "/secao",
                HOME,
                "section",
                PageStatus.OK,
                article_urls=(other,),
                checked_at=NOW,
            )
        ).suggested
        == ()
    )


def test_orcamento_inclui_robots_e_filhos_e_nao_grava_tentativa_nao_feita():
    from tests.strategy.helpers import sitemap_index

    root, child, other = HOME + "/index.xml", HOME + "/child.xml", HOME + "/other.xml"
    fetcher = FakeFetcher(
        {
            HOME + "/robots.txt": robots(),
            root: sitemap_index((child, 1)),
            child: plain_sitemap(("economia/materia-qualquer-do-jornal", 1)),
        }
    )
    checks = audit_sources(
        make_site(), fetcher, [root, other], user_agent=UA, now=NOW, max_requests=2
    )
    assert len(fetcher.calls) == 2
    assert other not in fetcher.calls
    assert len(checks) == 1 and checks[0].partial


def test_robots_de_subdominio_tambem_e_respeitado():
    other = "https://editoria.exemplo.com/feed.xml"
    fetcher = FakeFetcher(
        {"https://editoria.exemplo.com/robots.txt": b"User-agent: *\nDisallow: /feed.xml\n"}
    )
    checks = audit_sources(make_site(), fetcher, [other], user_agent=UA, now=NOW)
    assert other not in fetcher.calls
    assert checks[0].verdict == "disallowed"


def test_correcao_manual_impede_a_leitura_do_endereco():
    url = HOME + "/nao-usar.xml"
    fetcher = FakeFetcher()
    checks = audit_sources(make_site(), fetcher, [url], user_agent=UA, now=NOW, exclude=(url,))
    assert checks == [] and fetcher.calls == []


def test_checagem_anterior_a_leitura_da_secao_nao_comprova_amostra_atual():
    from datetime import timedelta

    url = HOME + "/sitemap.xml"
    article = HOME + "/politica/materia-com-titulo-do-jornal"
    fetcher = FakeFetcher(
        {
            HOME + "/robots.txt": robots(),
            url: plain_sitemap(("politica/materia-com-titulo-do-jornal", 1)),
        }
    )
    checks = audit_sources(make_site(), fetcher, [url], user_agent=UA, now=NOW - timedelta(hours=1))
    page = MapPage(
        HOME + "/politica", HOME, "section", PageStatus.OK, article_urls=(article,), checked_at=NOW
    )
    assert compare_page(page, checks).status == "pending"


def test_todas_as_materias_listadas_inclusive_sem_data_servem_para_conferencia():
    url = HOME + "/sitemap.xml"
    fetcher = FakeFetcher(
        {
            HOME + "/robots.txt": robots(),
            url: plain_sitemap(("economia/materia-numero-um-do-jornal", None)),
        }
    )
    checks = audit_sources(make_site(), fetcher, [url], user_agent=UA, now=NOW)
    page = MapPage(
        HOME + "/economia",
        HOME,
        "section",
        PageStatus.OK,
        article_urls=(HOME + "/economia/materia-numero-um-do-jornal",),
        checked_at=NOW,
    )
    assert compare_page(page, checks).status == "sample_full"


def test_pagina_indice_html_revela_feeds_sem_ser_tratada_como_feed_de_materias():
    index, feed = HOME + "/rss.html", HOME + "/politica.xml"
    fetcher = FakeFetcher(
        {
            HOME + "/robots.txt": robots(),
            index: b'<html><a href="/politica.xml">RSS</a></html>',
            feed: plain_sitemap(("politica/materia-com-titulo-do-jornal", 1)),
        }
    )
    checks = audit_sources(make_site(), fetcher, [index], user_agent=UA, now=NOW)
    assert checks[0].kind == checks[0].verdict == "feed_index"
    assert checks[0].listed_urls == (feed,)
    assert checks[1].kind == "sitemap"
    page = MapPage(
        HOME,
        "",
        "root",
        PageStatus.OK,
        article_urls=(HOME + "/politica/materia-com-titulo-do-jornal",),
        checked_at=NOW,
    )
    assert compare_page(page, checks).status == "sample_full"
    assert compare_page(page, checks[:1]).status == "pending"


def test_checkpoint_por_mecanismo_inclui_erro_de_orcamento_para_retomada():
    from tests.strategy.helpers import sitemap_index

    index, first, second = HOME + "/index.xml", HOME + "/first.xml", HOME + "/second.xml"
    fetcher = FakeFetcher(
        {
            HOME + "/robots.txt": robots(),
            index: sitemap_index((first, 2), (second, 1)),
            first: plain_sitemap(("politica/materia-com-titulo-do-jornal", 1)),
            second: plain_sitemap(("politica/outra-materia-do-jornal", 1)),
        }
    )
    recorded = []
    checks = audit_sources(
        make_site(),
        fetcher,
        [index],
        user_agent=UA,
        now=NOW,
        max_requests=3,
        on_source=recorded.append,
    )
    assert recorded == checks
    assert checks[0].partial
    assert "orçamento" in checks[0].detail


def test_planejamento_retoma_orcamento_e_feeds_descobertos_sem_reler_indice_conferido():
    index, feed, partial = HOME + "/rss.html", HOME + "/feed.xml", HOME + "/index.xml"
    page = MapPage(
        HOME, "", "root", PageStatus.OK, feeds=(partial,), feed_indexes=(index,), checked_at=NOW
    )
    previous = [
        MapSource(index, "feed_index", "feed_index", (feed,), NOW),
        MapSource(
            partial,
            "unavailable",
            None,
            (),
            NOW,
            partial=True,
            detail="orçamento de mapeamento esgotado",
        ),
    ]
    planned = plan_source_audit(make_site(), [page], previous, [], now=NOW)
    assert planned == [feed, partial]
    assert index not in planned


def test_planejamento_reavalia_falha_somente_quando_pedido_ou_vencida_a_espera():
    url = HOME + "/feed.xml"
    page = MapPage(HOME, "", "root", PageStatus.OK, feeds=(url,), checked_at=NOW)
    previous = [MapSource(url, "blocked", None, (), NOW)]
    assert plan_source_audit(make_site(), [page], previous, [], now=NOW) == []
    assert plan_source_audit(make_site(), [page], previous, [], now=NOW, retry_failed=True) == [url]


def test_falha_recente_na_pagina_nao_invalida_leitura_de_mecanismo_ainda_atual():
    from datetime import timedelta

    url = HOME + "/feed.xml"
    page = MapPage(HOME, "", "root", PageStatus.ERROR, feeds=(url,), checked_at=NOW)
    previous = [MapSource(url, "fresh", "feed", (), NOW - timedelta(hours=1))]
    assert plan_source_audit(make_site(), [page], previous, [], now=NOW) == []


def test_sondagem_normal_nao_retem_todas_as_urls_de_arquivo_na_memoria():
    from buscador_midias.discovery.robots import RobotsPolicy
    from buscador_midias.domain.strategy import CandidateOrigin
    from buscador_midias.strategy.evaluate import evaluate_candidate

    url = HOME + "/arquivo.xml"
    fetcher = FakeFetcher({url: plain_sitemap(("noticia-antiga", 24 * 365))})
    report = evaluate_candidate(
        url,
        CandidateOrigin.HINT,
        site=make_site(),
        fetcher=fetcher,
        robots=RobotsPolicy.allow_all(),
        user_agent=UA,
        now=NOW,
    )
    assert report.listed_urls == frozenset()
