from dataclasses import replace

from tests.strategy.helpers import HOME, NOW, UA, FakeFetcher, make_site, robots, status
from tests.strategy.test_listing import art, page

from buscador_midias.domain.mapping import MapPage, MapSource, PageStatus
from buscador_midias.net.fetcher import FetchResult
from buscador_midias.strategy.mapping import drain_site, map_site, sections_from_sources


class MemoryMap:
    def __init__(self):
        self.pages = {}

    def mapping_pages(self, site_key):
        return list(self.pages.get(site_key, {}).values())

    def save_mapping_pages(self, site_key, pages):
        self.pages.setdefault(site_key, {}).update({p.url: p for p in pages})


def run(fetcher, store, **kwargs):
    return map_site(make_site(), fetcher, store, user_agent=UA, now=NOW, **kwargs)


def world():
    return FakeFetcher(
        {
            HOME + "/robots.txt": robots(),
            HOME: page("/mundo", "/economia", "/tag/brasil", "/login"),
            HOME + "/mundo": page("/mundo/africa", art(1, "mundo")),
            HOME + "/economia": page(art(2, "economia")),
            HOME + "/mundo/africa": page(art(3, "mundo/africa")),
        }
    )


def test_drain_consumes_new_subsections_without_revisiting_pages():
    store, fetcher = MemoryMap(), world()
    batches = []
    result = drain_site(
        make_site(),
        fetcher,
        store,
        user_agent=UA,
        clock=lambda: NOW,
        batch_pages=1,
        on_batch=batches.append,
    )
    assert result.pending == 0 and result.visited == 4
    assert len(batches) == 4
    assert fetcher.calls.count(HOME) == 1


def test_drain_stops_on_unreadable_robots_preserving_the_pending_queue():
    store, fetcher = MemoryMap(), world()
    run(fetcher, store, max_pages=1)
    fetcher.responses[HOME + "/robots.txt"] = status(HOME + "/robots.txt", 403)
    fetcher.calls.clear()
    result = drain_site(
        make_site(), fetcher, store, user_agent=UA, clock=lambda: NOW, batch_pages=1
    )
    assert result.pending == 2 and result.detail
    assert fetcher.calls == [HOME + "/robots.txt"]


def test_drain_does_not_repeat_failures_between_batches():
    store, fetcher = MemoryMap(), world()
    fetcher.responses[HOME + "/mundo"] = status(HOME + "/mundo", 503)
    result = drain_site(
        make_site(),
        fetcher,
        store,
        user_agent=UA,
        clock=lambda: NOW,
        batch_pages=1,
        retry_failed=True,
    )
    assert result.pending == 0 and result.issues == 1
    assert fetcher.calls.count(HOME + "/mundo") == 1


def test_repeated_connection_failures_pause_drain_preserving_unattempted_pages():
    paths = [f"/secao-{name}" for name in ("a", "b", "c", "d", "e")]
    fetcher = FakeFetcher({HOME + "/robots.txt": robots(), HOME: page(*paths)})
    for path in paths:
        fetcher.responses[HOME + path] = FetchResult(
            HOME + path, None, b"", "ConnectError: Temporary failure in name resolution"
        )
    store = MemoryMap()
    result = drain_site(make_site(), fetcher, store, user_agent=UA, clock=lambda: NOW)
    assert result.pending == 2 and "suspensa" in result.detail
    assert sum(HOME + path in fetcher.calls for path in paths) == 3
    root = next(p for p in store.mapping_pages(make_site().key) if p.kind == "root")
    assert root.status is PageStatus.OK and "fila preservada" in root.detail
    for path in paths:
        fetcher.responses[HOME + path] = page()
    resumed = drain_site(make_site(), fetcher, store, user_agent=UA, clock=lambda: NOW)
    assert resumed.pending == 0 and not resumed.detail
    assert not next(p for p in store.mapping_pages(make_site().key) if p.kind == "root").detail


def test_missing_urls_do_not_pause_other_sections():
    paths = [f"/secao-{name}" for name in ("a", "b", "c", "d", "e")]
    fetcher = FakeFetcher({HOME + "/robots.txt": robots(), HOME: page(*paths)})
    fetcher.responses.update({HOME + path: status(HOME + path, 404) for path in paths})
    result = drain_site(make_site(), fetcher, MemoryMap(), user_agent=UA, clock=lambda: NOW)
    assert result.pending == 0 and not result.detail
    assert all(HOME + path in fetcher.calls for path in paths)


def test_topic_directories_are_taxonomies_in_new_and_resumed_queues():
    store = MemoryMap()
    previous = MapPage(HOME + "/topic/alguem", HOME, "section", title="Preservar")
    store.save_mapping_pages(make_site().key, [
        previous, MapPage(HOME + "/en/persons/alguem", HOME, "section"),
    ])
    fetcher = FakeFetcher({
        HOME + "/robots.txt": robots(),
        HOME: page("/topics-list/a", "/topic/brasil", "/politica"),
        HOME + "/politica": page(art(1, "politica")),
    })
    result = run(fetcher, store)
    assert result.pending == 0 and result.visited == 2
    saved = {p.url: p for p in store.mapping_pages(make_site().key)}
    for path in ("/topic/alguem", "/topic/brasil", "/topics-list/a", "/en/persons/alguem"):
        assert saved[HOME + path].page_type == "taxonomy"
        assert saved[HOME + path].status is PageStatus.EXCLUDED
        assert HOME + path not in fetcher.calls
    assert saved[previous.url].title == previous.title


def test_catalog_entry_with_topic_path_is_not_silently_excluded():
    site = replace(make_site(), url=HOME + "/topic/brasil", path_prefix="/topic/brasil")
    fetcher = FakeFetcher({HOME + "/robots.txt": robots(), site.url: page()})
    store = MemoryMap()
    result = map_site(site, fetcher, store, user_agent=UA, now=NOW)
    assert result.visited == 1 and site.url in fetcher.calls


def test_relative_category_loop_is_preserved_for_review_without_expanding_forever():
    first = HOME + "/category/analiza"
    second = HOME + "/category/category/analiza"
    third = HOME + "/category/category/category/analiza"
    legacy = HOME + "/category/category/category/category/libraria"
    store = MemoryMap()
    store.save_mapping_pages(make_site().key, [MapPage(legacy, third, "section")])
    fetcher = FakeFetcher({
        HOME + "/robots.txt": robots(), HOME: page(first),
        first: page("category/analiza"), second: page("category/analiza"),
    })
    result = drain_site(
        make_site(), fetcher, store, user_agent=UA, clock=lambda: NOW, batch_pages=1
    )
    assert result.pending == 0 and result.visited == 3
    saved = {p.url: p for p in store.mapping_pages(make_site().key)}
    for url in (third, legacy):
        assert saved[url].status is PageStatus.EXCLUDED
        assert "ciclo" in saved[url].detail and url not in fetcher.calls


def test_malformed_ipv6_link_does_not_abort_the_source_or_lose_valid_links():
    store = MemoryMap()
    fetcher = FakeFetcher({
        HOME + "/robots.txt": robots(),
        HOME: page("https://[invalid/feed", "/politica"),
        HOME + "/politica": page(art(1, "politica")),
    })
    result = run(fetcher, store)
    assert result.visited == 2 and result.pending == 0


def test_repeated_index_segments_also_suspend_new_and_persisted_navigation_cycles():
    old = HOME + "/index/pays/" + "index/" * 20 + "sujet"
    new = HOME + "/index/index/index/pays"
    store = MemoryMap()
    store.save_mapping_pages(make_site().key, [MapPage(old, HOME, "section")])
    fetcher = FakeFetcher({HOME + "/robots.txt": robots(), HOME: page(new)})
    result = run(fetcher, store)
    assert result.visited == 1 and result.pending == 0
    for url in (old, new):
        saved = next(p for p in store.mapping_pages(make_site().key) if p.url == url)
        assert saved.status is PageStatus.EXCLUDED and "ciclo" in saved.detail
        assert url not in fetcher.calls


def test_mecanismos_revelam_prefixos_sem_incluir_datas_taxonomias_servicos_ou_outro_host():
    check = MapSource(
        HOME + "/feed.xml",
        "fresh",
        "feed",
        (
            HOME + "/politica/brasil/materia-com-titulo-do-jornal",
            HOME + "/politica/brasil/outra-materia-com-titulo-longo",
            HOME + "/mundo/2026/10/17/materia-com-titulo-do-jornal",
            HOME + "/author/alguem/materia-com-titulo-do-jornal",
            HOME + "/login/materia-com-titulo-do-jornal",
            "https://outro.com/economia/materia-com-titulo-do-jornal",
        ),
        NOW,
    )
    existing = [MapPage(HOME + "/politica", HOME, "section", PageStatus.OK)]
    found = sections_from_sources(make_site(), existing, [check])
    assert {p.url for p in found} == {HOME + "/politica/brasil", HOME + "/mundo"}
    assert all(p.found_on == check.url and p.status is PageStatus.PENDING for p in found)
    assert all(p.page_type == "unknown" and p.discovery_basis == "mechanism_path" for p in found)
    assert sections_from_sources(make_site(), existing + found, [check]) == []


def test_prefixo_derivado_respeita_escopo_e_nao_reabre_paginas_excluidas():
    site = replace(make_site(), url=HOME + "/ac/acre", path_prefix="/ac/acre")
    root = MapPage(site.url, "", "root", PageStatus.OK)
    excluded = MapPage(site.url + "/economia", site.url, "section", PageStatus.EXCLUDED)
    check = MapSource(
        HOME + "/sitemap.xml",
        "fresh",
        "sitemap",
        (
            site.url + "/economia/materia-com-titulo-do-jornal",
            site.url + "/politica/materia-com-titulo-do-jornal",
            HOME + "/sp/saopaulo/materia-com-titulo-do-jornal",
        ),
        NOW,
    )
    assert [p.url for p in sections_from_sources(site, [root, excluded], [check])] == [
        site.url + "/politica"
    ]
    assert sections_from_sources(site, [root], [replace(check, verdict="error")]) == []
    assert sections_from_sources(site, [root], [replace(check, kind="feed_index")]) == []


def test_raiz_proibida_nao_impede_subsecao_explicitamente_permitida_no_robots():
    fetcher, store = world(), MemoryMap()
    store.save_mapping_pages(
        "exemplo.com",
        [
            MapPage(HOME, "", "root"),
            MapPage(HOME + "/politica", HOME + "/feed.xml", "section"),
        ],
    )
    fetcher.responses[HOME + "/robots.txt"] = b"User-agent: *\nAllow: /politica\nDisallow: /\n"
    fetcher.responses[HOME + "/politica"] = page(art(1, "politica"))
    result = run(fetcher, store)
    assert result.pending == 0 and result.visited == 1
    assert HOME not in fetcher.calls and HOME + "/politica" in fetcher.calls
    assert store.pages["exemplo.com"][HOME].status is PageStatus.DISALLOWED


def test_feed_com_identificador_curto_tambem_revela_caminho_candidato():
    source = MapSource(
        HOME + "/feed.xml", "fresh", "feed", (HOME + "/news/articles/czxyab7qexl9",), NOW
    )
    result = sections_from_sources(make_site(), [], [source])
    assert [p.url for p in result] == [HOME + "/news", HOME + "/news/articles"]
    assert all(p.page_type == "unknown" for p in result)


def test_resume_a_fila_sem_reler_paginas_e_preserva_subsecoes():
    store, fetcher = MemoryMap(), world()
    first = run(fetcher, store, max_pages=1)
    assert first.pending == 2
    assert store.pages["exemplo.com"][HOME + "/mundo"].found_on == HOME
    second = run(fetcher, store, max_pages=2)
    assert second.pending == 1
    assert HOME + "/mundo/africa" in store.pages["exemplo.com"]
    third = run(fetcher, store, max_pages=2)
    assert third.pending == 0
    assert fetcher.calls.count(HOME) == 1
    assert third.sections == 3
    assert third.visited == 4


def test_taxonomias_ficam_explicitamente_excluidas_e_servicos_nao_entram():
    store, fetcher = MemoryMap(), world()
    run(fetcher, store)
    pages = store.pages["exemplo.com"]
    assert pages[HOME + "/tag/brasil"].status is PageStatus.EXCLUDED
    assert HOME + "/tag/brasil" not in fetcher.calls
    assert pages[HOME + "/login"].page_type == "service"
    assert pages[HOME + "/login"].status is PageStatus.EXCLUDED
    assert HOME + "/login" not in fetcher.calls


def test_robots_e_falhas_ficam_no_inventario_e_so_sao_retentados_se_pedido():
    fetcher, store = world(), MemoryMap()
    fetcher.responses[HOME + "/robots.txt"] = b"User-agent: *\nDisallow: /economia\n"
    fetcher.responses[HOME + "/mundo"] = status(HOME + "/mundo", 403)
    summary = run(fetcher, store)
    assert summary.issues == 2
    assert HOME + "/economia" not in fetcher.calls
    run(fetcher, store)
    assert fetcher.calls.count(HOME + "/mundo") == 1
    run(fetcher, store, retry_failed=True)
    assert fetcher.calls.count(HOME + "/mundo") == 2


def test_feed_declarado_fica_associado_a_secao_sem_ser_baixado():
    store, fetcher = MemoryMap(), world()
    fetcher.responses[HOME + "/economia"] = (
        b'<html><head><link rel="alternate" type="application/rss+xml" '
        b'href="/rss/economia.xml"></head><body></body></html>'
    )
    run(fetcher, store)
    mapped = store.pages["exemplo.com"][HOME + "/economia"]
    assert mapped.feeds == (HOME + "/rss/economia.xml",)
    assert HOME + "/rss/economia.xml" not in fetcher.calls


def test_erro_em_robots_nao_apaga_inventario_ou_visita_paginas():
    store, fetcher = MemoryMap(), world()
    run(fetcher, store, max_pages=1)
    previous = dict(store.pages["exemplo.com"])
    fetcher.responses[HOME + "/robots.txt"] = status(HOME + "/robots.txt", 403)
    fetcher.calls.clear()
    summary = run(fetcher, store)
    assert summary.detail
    assert summary.issues == 1
    assert store.pages["exemplo.com"][HOME + "/mundo"] == previous[HOME + "/mundo"]
    assert store.pages["exemplo.com"][HOME].detail == summary.detail
    assert fetcher.calls == [HOME + "/robots.txt"]


def test_redirecionamento_para_outro_site_nao_polui_o_mapa():
    store, fetcher = MemoryMap(), world()
    fetcher.responses[HOME + "/mundo"] = replace(
        status(HOME + "/mundo", 200),
        content=page("/inventada"),
        final_url="https://outro.com",
    )
    run(fetcher, store)
    assert store.pages["exemplo.com"][HOME + "/mundo"].status is PageStatus.INVALID
    assert not any("inventada" in url for url in store.pages["exemplo.com"])


def test_desafio_com_status_200_nao_conta_como_pagina_explorada():
    store, fetcher = MemoryMap(), world()
    fetcher.responses[HOME + "/mundo"] = b"<html><title>Just a moment</title></html>"
    summary = run(fetcher, store)
    assert store.pages["exemplo.com"][HOME + "/mundo"].status is PageStatus.BLOCKED
    assert summary.issues == 1


def test_metadado_article_sozinho_nao_descarta_categorias_como_as_do_m28():
    store, fetcher = MemoryMap(), world()
    fetcher.responses[HOME + "/mundo"] = (
        b'<html><head><meta property="og:type" content="article"></head><body>'
        + page(art(1, "mundo"), "/mundo/africa")
        + b"</body></html>"
    )
    run(fetcher, store)
    mapped = store.pages["exemplo.com"][HOME + "/mundo"]
    assert mapped.status is PageStatus.OK
    assert "confirmar" in mapped.detail
    assert HOME + "/mundo/africa" in store.pages["exemplo.com"]


def test_inventario_nao_trunca_mais_de_40_secoes():
    store = MemoryMap()
    sections = ["/secao" + chr(97 + i // 26) + chr(97 + i % 26) for i in range(65)]
    fetcher = FakeFetcher({HOME + "/robots.txt": robots(), HOME: page(*sections)})
    summary = run(fetcher, store, max_pages=1)
    assert summary.sections == 65
    assert summary.pending == 65


def test_amostra_da_secao_nao_inclui_materias_do_menu_e_rodape():
    store, fetcher = MemoryMap(), world()
    fetcher.responses[HOME] = (
        f'<html><nav><a href="{art(1)}">Menu</a><a href="/politica">Política</a></nav>'
        f'<main><a href="{art(2)}">Matéria</a></main>'
        f'<footer><a href="{art(3)}">Rodapé</a></footer></html>'
    ).encode()
    run(fetcher, store, max_pages=1)
    assert store.pages["exemplo.com"][HOME].article_urls == (HOME + art(2),)
    assert HOME + "/politica" in store.pages["exemplo.com"]


def test_refresh_rele_paginas_concluidas_e_atualiza_a_amostra():
    store, fetcher = MemoryMap(), world()
    run(fetcher, store, max_pages=1)
    fetcher.responses[HOME] = page(art(99))
    run(fetcher, store, max_pages=1, refresh_pages=True)
    assert fetcher.calls.count(HOME) == 2
    assert store.pages["exemplo.com"][HOME].article_urls == (HOME + art(99),)


def test_varredura_respeita_robots_do_subdominio_em_vez_de_reusar_o_da_home():
    store, fetcher = MemoryMap(), world()
    url = "https://editoria.exemplo.com/politica"
    fetcher.responses[HOME] = page(url)
    fetcher.responses["https://editoria.exemplo.com/robots.txt"] = (
        b"User-agent: *\nDisallow: /politica\n"
    )
    run(fetcher, store)
    assert url not in fetcher.calls
    assert store.pages["exemplo.com"][url].status is PageStatus.DISALLOWED


def test_materia_disfarcada_de_secao_nao_expande_a_fila():
    import json

    store, fetcher = MemoryMap(), world()
    data = {
        "@type": "NewsArticle",
        "url": HOME + "/mundo",
        "headline": "Uma notícia",
        "datePublished": "2026-10-17",
    }
    fetcher.responses[HOME + "/mundo"] = (
        f'<html><script type="application/ld+json">{json.dumps(data)}</script>'
        '<a href="/nao-expandir">link</a></html>'
    ).encode()
    run(fetcher, store)
    mapped = store.pages["exemplo.com"][HOME + "/mundo"]
    assert mapped.page_type == "article" and mapped.status is PageStatus.EXCLUDED
    assert HOME + "/nao-expandir" not in store.pages["exemplo.com"]


def test_link_de_navegacao_com_html_e_candidata_mesmo_sem_formato_de_pasta():
    store, fetcher = MemoryMap(), world()
    fetcher.responses[HOME] = (
        '<html><nav><a href="/politica/index.html">Política</a></nav></html>'.encode()
    )
    run(fetcher, store, max_pages=1)
    candidate = store.pages["exemplo.com"][HOME + "/politica/index.html"]
    assert candidate.discovery_basis == "navigation"
    assert candidate.page_type == "unknown"


def test_nova_evidencia_de_navegacao_nao_apaga_pagina_ja_lida():
    store, fetcher = MemoryMap(), world()
    run(fetcher, store)
    previous = store.pages["exemplo.com"][HOME + "/mundo"]
    fetcher.responses[HOME] = b'<html><nav><a href="/mundo">Mundo</a></nav></html>'
    run(fetcher, store, max_pages=1, refresh_pages=True)
    current = store.pages["exemplo.com"][HOME + "/mundo"]
    assert current.discovery_basis == "navigation"
    assert current.status == previous.status and current.checked_at == previous.checked_at
    assert current.article_urls == previous.article_urls


def test_navegacao_explicita_tem_prioridade_sobre_palpite_de_formato_de_url():
    store, fetcher = MemoryMap(), world()
    root = MapPage(HOME, "", "root", PageStatus.OK)
    guessed = MapPage(HOME + "/palpite", HOME, "section")
    navigation = MapPage(HOME + "/politica", HOME, "section", discovery_basis="navigation")
    store.save_mapping_pages("exemplo.com", [root, guessed, navigation])
    fetcher.responses[HOME + "/politica"] = page()
    run(fetcher, store, max_pages=1)
    assert HOME + "/politica" in fetcher.calls
    assert HOME + "/palpite" not in fetcher.calls


def test_subsecao_profunda_nao_e_descartada_por_ter_mais_de_tres_segmentos():
    store, fetcher = MemoryMap(), world()
    path = "/brasil/norte/acre/politica/congresso"
    fetcher.responses[HOME] = page(path)
    run(fetcher, store, max_pages=1)
    assert store.pages["exemplo.com"][HOME + path].status is PageStatus.PENDING


def test_fonte_de_secao_nao_segue_links_fora_do_prefixo():
    site, store = make_site(HOME + "/ac/acre"), MemoryMap()
    fetcher = FakeFetcher(
        {
            HOME + "/robots.txt": robots(),
            site.url: page("/sp", "/ac/acre/politica"),
        }
    )
    map_site(site, fetcher, store, user_agent=UA, now=NOW, max_pages=1)
    assert [p.url for p in store.mapping_pages(site.key)] == [
        site.url,
        HOME + "/ac/acre/politica",
    ]


def test_pagina_persistida_com_nul_e_associacoes():
    from buscador_midias.state.store import StateStore

    store = StateStore()
    try:
        mapped = MapPage(
            url=HOME,
            found_on="",
            kind="root",
            status=PageStatus.OK,
            title="Título\x00",
            article_urls=(HOME + art(1),),
            feeds=(HOME + "/rss.xml",),
            checked_at=NOW,
            attempts=1,
            page_type="section",
            type_basis="self_collection_schema",
            discovery_basis="entrypoint",
        )
        store.save_mapping_pages("exemplo.com", [mapped])
        store.save_mapping_pages("exemplo.com", [replace(mapped, title="Título")])
        assert store.mapping_pages("exemplo.com") == [replace(mapped, title="Título")]
    finally:
        store.close()
