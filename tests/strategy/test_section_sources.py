"""Fontes por seção: feeds declarados nas páginas, páginas-índice de feeds e padrões de URL."""

from tests.strategy.helpers import HOME, make_site

from buscador_midias.strategy.section_sources import (
    apply_templates,
    derive_templates,
    find_feed_sources,
)


def html(*parts: str) -> bytes:
    return f"<html><head>{parts[0]}</head><body>{''.join(parts[1:])}</body></html>".encode()


def link(href: str) -> str:
    return f'<link rel="alternate" type="application/rss+xml" href="{href}">'


def a(href: str) -> str:
    return f'<a href="{href}">x</a>'


# --- o que uma página aponta ---


def test_separa_feeds_declarados_links_diretos_e_paginas_indice_de_feeds():
    page = html(
        link("/feed/economia"),
        a("/rss/geral.xml"),
        a("/feeds/politica/"),
        a("/tec/2013/lista-de-feeds-do-site.shtml"),
        a("/feedback"),
        a("/about"),
    )

    found = find_feed_sources(page, f"{HOME}/", make_site())

    assert found.declared == (f"{HOME}/feed/economia",)
    assert f"{HOME}/rss/geral.xml" in found.direct
    assert found.indexes == (f"{HOME}/tec/2013/lista-de-feeds-do-site.shtml",)
    assert f"{HOME}/feeds/politica/" in found.direct + found.indexes
    assert all("feedback" not in u and "about" not in u for u in found.direct + found.indexes)


def test_feed_de_comentarios_e_de_outro_dominio_ficam_de_fora():
    page = html(
        link("/comments/feed"),
        link("https://outro.com/feed"),
        a("/post/comments/feed.xml"),
        a("https://outro.com/rss.xml"),
    )

    found = find_feed_sources(page, f"{HOME}/", make_site())

    assert found.declared == () and found.direct == () and found.indexes == ()


def test_subdominio_do_site_vale_como_do_site():
    page = html("", a("https://feeds.exemplo.com/politica/rss.xml"))

    found = find_feed_sources(page, f"{HOME}/", make_site())

    assert found.direct == ("https://feeds.exemplo.com/politica/rss.xml",)


def test_sem_repeticao_e_na_ordem_em_que_aparecem():
    page = html(link("/feed"), link("/feed"), a("/rss.xml"), a("/rss.xml"))

    found = find_feed_sources(page, f"{HOME}/", make_site())

    assert found.declared == (f"{HOME}/feed",) and found.direct == (f"{HOME}/rss.xml",)


# --- padrões de URL ---

SECTIONS = (f"{HOME}/economia", f"{HOME}/politica", f"{HOME}/ac/acre", f"{HOME}/mundo")


def test_a_url_que_traz_o_caminho_de_uma_secao_vira_um_modelo():
    templates = derive_templates([f"{HOME}/rss/g1/economia/"], SECTIONS)

    assert templates == (f"{HOME}/rss/g1/{{section}}/",)


def test_secao_de_dois_segmentos_tambem_gera_modelo():
    templates = derive_templates([f"{HOME}/rss/g1/ac/acre/"], SECTIONS)

    assert templates == (f"{HOME}/rss/g1/{{section}}/",)


def test_segmento_que_so_parece_com_a_secao_pela_metade_nao_gera_modelo():
    assert derive_templates([f"{HOME}/rss/economia-global/"], SECTIONS) == ()
    assert derive_templates([f"{HOME}/rss/"], SECTIONS) == ()


def test_modelos_repetidos_aparecem_uma_vez():
    urls = [f"{HOME}/rss/g1/economia/", f"{HOME}/rss/g1/politica/"]

    assert derive_templates(urls, SECTIONS) == (f"{HOME}/rss/g1/{{section}}/",)


def test_aplicar_os_modelos_as_outras_secoes_sem_repetir_o_que_ja_se_conhece():
    templates = (f"{HOME}/rss/g1/{{section}}/",)
    known = {f"{HOME}/rss/g1/economia/"}

    urls = apply_templates(templates, SECTIONS, known, limit=10)

    assert urls == [
        f"{HOME}/rss/g1/politica/",
        f"{HOME}/rss/g1/ac/acre/",
        f"{HOME}/rss/g1/mundo/",
    ]


def test_o_limite_de_urls_geradas_vale():
    urls = apply_templates((f"{HOME}/rss/{{section}}/",), SECTIONS, set(), limit=2)

    assert len(urls) == 2
