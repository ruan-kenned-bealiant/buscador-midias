import json

from tests.strategy.helpers import HOME

from buscador_midias.strategy.page_classification import classify_page, classify_url


def page(data, *, meta="", heading=""):
    return (
        f'<html><head>{meta}<script type="application/ld+json">'
        f"{json.dumps(data)}</script></head><body><h1>{heading}</h1></body></html>"
    ).encode()


def test_materia_com_slug_curto_e_schema_da_propria_url_nao_e_secao():
    url = HOME + "/mundo/guerra"
    result = classify_page(
        page(
            {
                "@type": "NewsArticle",
                "url": url,
                "headline": "Guerra",
                "datePublished": "2026-10-17",
            }
        ),
        url,
    )
    assert result.page_type == "article"
    assert result.basis == "self_article_schema"


def test_og_article_isolado_como_no_m28_nao_rejeita_categoria():
    result = classify_page(
        page({}, meta='<meta property="og:type" content="article">'), HOME + "/category/articles"
    )
    assert result.page_type == "unknown"


def test_article_de_destaque_dentro_da_secao_nao_muda_o_tipo_da_pagina():
    result = classify_page(
        page(
            {
                "@type": "NewsArticle",
                "url": HOME + "/noticia",
                "headline": "Notícia",
                "datePublished": "2026-10-17",
            }
        ),
        HOME + "/politica",
    )
    assert result.page_type == "unknown"


def test_collection_page_confirma_secao_e_graph_e_lista_de_tipos_sao_aceitos():
    url = HOME + "/politica"
    result = classify_page(
        page({"@graph": [{"@type": ["WebPage", "CollectionPage"], "@id": url + "#webpage"}]}), url
    )
    assert result.page_type == "section"
    assert result.basis == "self_collection_schema"


def test_servico_da_propria_url_e_identificado_sem_confundir_noticia_sobre_privacidade():
    assert classify_url(HOME + "/privacy-policy").page_type == "service"
    assert classify_url(HOME + "/mundo/privacy-policy").page_type == "unknown"
    assert (
        classify_page(
            page({"@type": "ContactPage", "url": HOME + "/fale-conosco"}), HOME + "/fale-conosco"
        ).page_type
        == "service"
    )


def test_diretorios_de_servico_observados_nao_excluem_editorias_de_outros_sites():
    services = (
        "https://tvguide.vg.no/program/between",
        "https://tempo.folha.uol.com.br/pa/belem",
        "https://www.euronews.com/weather/asia/pakistan/islamabad",
        "https://hu.euronews.com/idojaras/europe/bulgaria",
        "https://fr.euronews.com/meteo/europe/royaume-uni/tenby",
        "https://de.euronews.com/wetter/asien/indien/warangal",
        "https://tr.euronews.com/hava-durumu/avrupa",
        "https://it.euronews.com/tempo/europa",
        "https://pt.euronews.com/meteorologia/europa",
        "https://es.euronews.com/tiempo/europa",
        "https://www.elnacional.com/lvbp/estadisticas/fildeo/orden/e/asc",
        "https://blogs.mediapart.fr/abirato/contacts",
        "https://www.nikkei.com/nkd/company/us/JADB/chart",
        "https://www.nikkei.com/nkd/company/us/HODU/history/dprice",
        "https://evene.lefigaro.fr/citations/theme/admiration",
        "https://evene.lefigaro.fr/celebre/biographie/mike-vogel",
        "https://correiodoestado.com.br/opiniao/coluna/atendimento/noticias-assinantes",
    )
    for url in services:
        result = classify_url(url)
        assert result.page_type == "service" and result.basis == "observed_service_route"
    for url in (
        HOME + "/weather",
        "https://www.euronews.com/news/weather",
        "https://hu.euronews.com/hirek/europa",
        "https://www.elnacional.com/deportes/lvbp",
        "https://blogs.mediapart.fr/abirato/blog/170126/contacts",
        "https://www.nikkei.com/nkd/company/us/JADB",
        "https://www.nikkei.com/nkd/company/us/JADB/news",
        "https://evene.lefigaro.fr/actualite/cinema",
        "https://www.lefigaro.fr/culture",
        "https://correiodoestado.com.br/opiniao/atendimento-publico",
        "https://correiodoestado.com.br/noticias-assinantes",
        "https://correiodoestado.com.br/noticias-assinantes/politica",
        "https://not-euronews.com/weather/asia",
    ):
        assert classify_url(url).page_type == "unknown"


def test_schema_sem_identidade_e_json_invalido_ficam_para_revisao():
    assert (
        classify_page(page({"@type": "Article", "headline": "Notícia"}), HOME + "/mundo").page_type
        == "unknown"
    )
    assert (
        classify_page(b'<script type="application/ld+json">{quebrado</script>', HOME).page_type
        == "unknown"
    )


def test_article_sem_identidade_usa_titulo_h1_e_data_so_quando_concordam():
    url = HOME + "/titulo-curto"
    data = {
        "@type": "NewsArticle",
        "headline": "Título específico da notícia",
        "datePublished": "2026-10-17",
    }
    good = page(
        data,
        heading="Título específico da notícia",
        meta='<meta property="og:type" content="article">',
    )
    assert classify_page(good, url).page_type == "article"
    assert classify_page(page(data, heading="Política"), url).page_type == "unknown"


def test_schema_conflitante_nao_exclui_uma_possivel_secao():
    url = HOME + "/politica"
    data = {
        "@graph": [
            {"@type": "CollectionPage", "url": url},
            {
                "@type": "NewsArticle",
                "url": url,
                "headline": "Política",
                "datePublished": "2026-10-17",
            },
        ]
    }
    result = classify_page(page(data), url)
    assert result.page_type == "unknown"
    assert result.basis == "conflicting_schema"


def test_identidade_do_destaque_tem_prioridade_sobre_id_local_do_bloco():
    url = HOME + "/politica"
    data = {
        "@type": "NewsArticle",
        "@id": url + "#destaque",
        "url": HOME + "/noticia",
        "headline": "Uma notícia",
        "datePublished": "2026-10-17",
    }
    assert classify_page(page(data), url).page_type == "unknown"
