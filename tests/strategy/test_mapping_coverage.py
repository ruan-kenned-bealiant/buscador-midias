from tests.strategy.helpers import HOME, NOW, UA, FakeFetcher, make_site, plain_sitemap, robots

from buscador_midias.domain.mapping import MapPage, MapSource, PageStatus
from buscador_midias.strategy.mapping_coverage import (
    audit_paused,
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


def test_audit_pauses_connection_failures_without_claiming_unattempted_endpoints():
    from buscador_midias.net.fetcher import FetchResult

    urls = [HOME + f"/feed-{i}.xml" for i in range(5)]
    fetcher = FakeFetcher({HOME + "/robots.txt": robots()})
    fetcher.responses.update({
        url: FetchResult(url, None, b"", "ConnectError: Temporary failure in name resolution")
        for url in urls
    })
    saved = []
    checks = audit_sources(
        make_site(), fetcher, urls, user_agent=UA, now=NOW, on_source=saved.append
    )
    assert len(checks) == 3 and checks == saved and audit_paused(checks)
    assert not set(urls[3:]).intersection(fetcher.calls)
    queue = plan_source_audit(make_site(), [], checks, [], now=NOW, hints=urls)
    assert queue == urls[3:]


def test_falha_de_feed_em_subdominio_nao_suspende_xml_de_outro_host():
    from buscador_midias.net.fetcher import FetchResult

    bad = [f"https://feeds.exemplo.com/feed-{n}.xml" for n in range(5)]
    good = HOME + "/sitemap.xml"
    fetcher = FakeFetcher({
        HOME + "/robots.txt": robots(), "https://feeds.exemplo.com/robots.txt": robots(),
        good: plain_sitemap(("politica/materia-com-titulo-do-jornal", 1)),
        **{u: FetchResult(u, None, b"", "ConnectError: DNS indisponivel") for u in bad},
    })
    suspended = {}
    checks = audit_sources(make_site(), fetcher, [*bad, good], user_agent=UA,
                           now=NOW, suspended_origins=suspended)
    assert len(checks) == 4 and checks[-1].kind == "sitemap"
    assert not set(bad[3:]).intersection(fetcher.calls)
    assert audit_paused(checks) and "feeds.exemplo.com" in suspended
    later = audit_sources(make_site(), fetcher, [bad[-1]], user_agent=UA,
                          now=NOW, suspended_origins=suspended)
    assert later == [] and bad[-1] not in fetcher.calls


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


def test_html_de_materia_sob_atom_nao_expande_fontes_e_corrige_indice_antigo():
    url = HOME + "/atom/art17007131-uma-materia-sobre-energia-nuclear"
    feed = HOME + "/rss.xml"
    fetcher = FakeFetcher({
        HOME + "/robots.txt": robots(),
        url: b'<html><head><link rel="alternate" type="application/rss+xml" '
             b'href="/rss.xml"></head><body>Uma materia</body></html>',
    })
    checks = audit_sources(make_site(), fetcher, [url], user_agent=UA, now=NOW)
    assert len(checks) == 1 and checks[0].kind == "html_article"
    assert checks[0].verdict == "invalid" and feed not in fetcher.calls
    legacy = MapSource(url, "feed_index", "feed_index", (feed,), NOW)
    normalized = normalize_source_audits([legacy])[0]
    assert normalized.kind == "html_article" and normalized.listed_urls == (feed,)
    assert normalized.checked_at == NOW
    root = MapPage(HOME, "", "root", PageStatus.OK, feeds=(url,), checked_at=NOW)
    assert plan_source_audit(
        make_site(), [root], [legacy], [], now=NOW, retry_failed=True,
    ) == []


def test_html_com_schema_da_propria_materia_nao_e_indice_mesmo_com_slug_curto():
    import json

    url = HOME + "/atom/energia"
    data = {"@type": "NewsArticle", "url": url, "headline": "Energia",
            "datePublished": "2026-10-17"}
    content = (
        '<html><head><script type="application/ld+json">' + json.dumps(data)
        + '</script><link rel="alternate" type="application/rss+xml" href="/rss.xml">'
        + '</head><body>Energia</body></html>'
    ).encode()
    fetcher = FakeFetcher({HOME + "/robots.txt": robots(), url: content})
    checks = audit_sources(make_site(), fetcher, [url], user_agent=UA, now=NOW)
    assert len(checks) == 1 and checks[0].kind == "html_article"
    assert HOME + "/rss.xml" not in fetcher.calls


def test_xml_real_em_url_com_aparencia_de_materia_prevalece_sobre_o_formato():
    url = HOME + "/atom/feed-especial-com-nome-de-categoria-longo"
    fetcher = FakeFetcher({
        HOME + "/robots.txt": robots(),
        url: plain_sitemap(("politica/materia-com-titulo-do-jornal", 1)),
    })
    check = audit_sources(make_site(), fetcher, [url], user_agent=UA, now=NOW)[0]
    assert check.kind == "sitemap" and check.verdict != "invalid"
    assert normalize_source_audits([check]) == [check]


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


def test_indice_preserva_links_de_taxonomia_sem_varrer_todos_os_feeds_de_palavras_chave():
    index, section, tag = HOME + "/rss.html", HOME + "/politica.xml", HOME + "/tema/alguem/feed"
    fetcher = FakeFetcher({
        HOME + "/robots.txt": robots(),
        index: f'<html><a href="{section}">RSS</a><a href="{tag}">Tag</a></html>'.encode(),
        section: plain_sitemap(("politica/materia-com-titulo-do-jornal", 1)),
        tag: plain_sitemap(("politica/outra-materia-do-jornal", 1)),
    })
    checks = audit_sources(make_site(), fetcher, [index], user_agent=UA, now=NOW)
    assert checks[0].listed_urls == (section, tag)
    assert tag not in fetcher.calls and section in fetcher.calls
    assert 'taxonomias' in checks[0].detail
    root = MapPage(HOME, "", "root", PageStatus.OK, feeds=(index,), checked_at=NOW)
    assert plan_source_audit(make_site(), [root], checks, [], now=NOW) == []
    assert plan_source_audit(make_site(), [root], checks, [], now=NOW, hints=(tag,)) == [tag]
    # Uma seção declara seu próprio feed: a rota de taxonomia não o impede.
    declared = MapPage(HOME + "/politica", HOME, "section", feeds=(tag,))
    assert plan_source_audit(make_site(), [root, declared], checks, [], now=NOW) == [tag]
    explicit = audit_sources(make_site(), fetcher, [tag], user_agent=UA, now=NOW)
    assert explicit[0].kind == "sitemap"


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
