from datetime import UTC, datetime

import pytest

from buscador_midias.discovery.feeds import (
    FeedSummary,
    find_feed_links,
    find_wordpress_api,
    parse_feed,
    parse_feed_entries,
)

BASE = "https://exemplo.com/"


def page(head: str) -> bytes:
    return f"<!doctype html><html><head><title>x</title>{head}</head><body></body></html>".encode()


def test_acha_os_feeds_declarados_na_home_com_url_absoluta():
    html = page(
        '<link rel="alternate" type="application/rss+xml" title="Feed" href="/feed/">'
        '<link rel="alternate" type="application/atom+xml" href="https://cdn.exemplo.com/atom.xml">'
    )

    assert find_feed_links(html, BASE) == (
        "https://exemplo.com/feed/",
        "https://cdn.exemplo.com/atom.xml",
    )


def test_ignora_o_que_nao_e_feed_de_noticias():
    html = page(
        '<link rel="stylesheet" type="text/css" href="/a.css">'
        '<link rel="alternate" hreflang="en" href="/en/">'
        '<link rel="alternate" type="application/rss+xml" title="Comments Feed"'
        ' href="/comments/feed/">'
        '<link rel="alternate" type="application/rss+xml" title="Notícias" href="/rss.xml">'
    )

    assert find_feed_links(html, BASE) == ("https://exemplo.com/rss.xml",)


def test_rel_com_varios_valores_atributos_em_caixa_alta_e_duplicatas():
    html = page(
        '<LINK REL="ALTERNATE home" TYPE="Application/RSS+XML" HREF="/rss">'
        '<link rel="alternate" type="application/rss+xml" href="/rss">'
    )

    assert find_feed_links(html, BASE) == ("https://exemplo.com/rss",)


@pytest.mark.parametrize("junk", [b"", b"nao e html <<<", b"\xff\xfe\x00"])
def test_html_ruim_nao_levanta(junk: bytes):
    assert find_feed_links(junk, BASE) == ()
    assert find_wordpress_api(junk, BASE) is None


def test_acha_a_api_rest_do_wordpress():
    html = page('<link rel="https://api.w.org/" href="https://exemplo.com/wp-json/">')

    assert find_wordpress_api(html, BASE) == "https://exemplo.com/wp-json/"
    assert find_wordpress_api(page(""), BASE) is None


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>a</title><pubDate>Thu, 15 Oct 2026 14:00:00 -0300</pubDate></item>
<item><title>b</title><pubDate>Thu, 15 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>c</title></item></channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
<entry><title>a</title><updated>2026-10-15T09:30:00Z</updated></entry>
<entry><title>b</title><published>2026-10-14T09:30:00+00:00</published></entry></feed>"""

RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>a</title><dc:date>2026-10-15T08:00:00Z</dc:date></item></rdf:RDF>"""


def test_le_rss_contando_itens_e_achando_a_data_mais_nova_em_utc():
    summary = parse_feed(RSS)

    assert summary == FeedSummary("rss", 3, datetime(2026, 10, 15, 17, 0, tzinfo=UTC))


def test_le_atom_e_rdf():
    assert parse_feed(ATOM) == FeedSummary("atom", 2, datetime(2026, 10, 15, 9, 30, tzinfo=UTC))
    assert parse_feed(RDF) == FeedSummary("rdf", 1, datetime(2026, 10, 15, 8, 0, tzinfo=UTC))


def test_feed_sem_datas_tem_data_nula():
    xml = b"<rss><channel><item><title>a</title></item></channel></rss>"

    assert parse_feed(xml) == FeedSummary("rss", 1, None)


@pytest.mark.parametrize(
    "xml",
    [
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>x</loc></url></urlset>',
        b"<html><body>pagina</body></html>",
        b"nem e xml",
        b"",
    ],
)
def test_o_que_nao_e_feed_devolve_none(xml: bytes):
    assert parse_feed(xml) is None


def test_entidade_externa_nao_e_resolvida():
    xml = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b"<rss><channel><item><title>&x;</title></item></channel></rss>"
    )

    summary = parse_feed(xml)

    assert summary is None or summary.items == 1  # nunca levanta e nunca lê o arquivo


RSS_ENTRIES = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Exemplo</title>
  <item><title>PF prende empresario</title><link>https://exemplo.com/politica/pf-prende</link>
        <pubDate>Thu, 15 Oct 2026 10:00:00 +0000</pubDate></item>
  <item><title> Sem data </title><link> https://exemplo.com/sem-data </link></item>
  <item><title>Sem link</title></item>
</channel></rss>"""

ATOM_ENTRIES = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Exemplo</title>
  <entry><title>Prefeito e alvo</title>
    <link rel="self" href="https://exemplo.com/api/1"/>
    <link rel="alternate" type="text/html" href="https://exemplo.com/cidade/prefeito-e-alvo"/>
    <updated>2026-10-15T09:30:00Z</updated></entry>
  <entry><title>Outra</title><link href="/relativa/outra"/>
    <published>2026-10-14T08:00:00-03:00</published></entry>
</feed>"""


def test_itens_do_rss_trazem_link_titulo_e_data():
    entries = parse_feed_entries(RSS_ENTRIES, BASE)

    assert [(e.url, e.title) for e in entries] == [
        ("https://exemplo.com/politica/pf-prende", "PF prende empresario"),
        ("https://exemplo.com/sem-data", "Sem data"),
    ]
    assert entries[0].published == datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    assert entries[1].published is None


def test_itens_do_atom_preferem_o_link_alternate_e_resolvem_endereco_relativo():
    entries = parse_feed_entries(ATOM_ENTRIES, BASE)

    assert [e.url for e in entries] == [
        "https://exemplo.com/cidade/prefeito-e-alvo",
        "https://exemplo.com/relativa/outra",
    ]
    assert entries[0].published == datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
    assert entries[1].published == datetime(2026, 10, 14, 11, 0, tzinfo=UTC)


@pytest.mark.parametrize("junk", [b"", b"<html><body>oi</body></html>", b"nao e xml <<<"])
def test_itens_de_conteudo_que_nao_e_feed_dao_lista_vazia(junk: bytes):
    assert parse_feed_entries(junk, BASE) == ()


def test_item_com_esquema_estranho_e_ignorado():
    xml = (
        b'<rss version="2.0"><channel>'
        b"<item><link>javascript:alert(1)</link></item>"
        b"<item><link>https://exemplo.com/ok-materia</link></item>"
        b"</channel></rss>"
    )

    assert [e.url for e in parse_feed_entries(xml, BASE)] == ["https://exemplo.com/ok-materia"]


@pytest.mark.parametrize("date", ["0001-01-01T00:00:00+04:00", "9999-12-31T23:59:59-04:00"])
def test_data_que_transborda_utc_nao_descarta_item_do_feed(date):
    xml = (
        '<rss><channel><item><title>Notícia</title><link>https://exemplo.com/reportagem</link>'
        f'<pubDate>{date}</pubDate></item></channel></rss>'
    ).encode()
    assert parse_feed(xml) == FeedSummary("rss", 1, None)
    entries = parse_feed_entries(xml, BASE)
    assert len(entries) == 1 and entries[0].url == "https://exemplo.com/reportagem"
    assert entries[0].published is None
