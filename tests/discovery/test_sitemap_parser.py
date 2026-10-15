import gzip
from datetime import UTC, datetime

import pytest

from buscador_midias.discovery.sitemap_parser import SitemapParseError, parse_sitemap
from buscador_midias.domain.sitemap import SitemapKind

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://exemplo.com/a</loc>
    <lastmod>2026-10-14T10:30:00+00:00</lastmod>
  </url>
  <url>
    <loc> https://exemplo.com/b </loc>
    <lastmod>2026-10-13</lastmod>
  </url>
  <url><loc>https://exemplo.com/c</loc></url>
</urlset>"""

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://exemplo.com/sitemap-2026-10.xml</loc>
    <lastmod>2026-10-14T00:00:00Z</lastmod>
  </sitemap>
  <sitemap><loc>https://exemplo.com/sitemap-2026-09.xml</loc></sitemap>
</sitemapindex>"""

NEWS = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
  <url>
    <loc>https://exemplo.com/politica/operacao</loc>
    <news:news>
      <news:publication>
        <news:name>Exemplo</news:name>
        <news:language>pt</news:language>
      </news:publication>
      <news:publication_date>2026-10-14T08:00:00-03:00</news:publication_date>
      <news:title>Operação prende diretor</news:title>
    </news:news>
  </url>
</urlset>""".encode()


def test_urlset_extrai_urls_e_datas():
    parsed = parse_sitemap(URLSET)

    assert parsed.kind is SitemapKind.URLSET
    assert [e.loc for e in parsed.entries] == [
        "https://exemplo.com/a",
        "https://exemplo.com/b",
        "https://exemplo.com/c",
    ]
    assert parsed.entries[0].lastmod == datetime(2026, 10, 14, 10, 30, tzinfo=UTC)
    assert parsed.entries[1].lastmod == datetime(2026, 10, 13, tzinfo=UTC)
    assert parsed.entries[2].lastmod is None
    assert parsed.children == ()
    assert parsed.has_news is False


def test_index_extrai_sitemaps_filhos():
    parsed = parse_sitemap(INDEX)

    assert parsed.kind is SitemapKind.INDEX
    assert [c.loc for c in parsed.children] == [
        "https://exemplo.com/sitemap-2026-10.xml",
        "https://exemplo.com/sitemap-2026-09.xml",
    ]
    assert parsed.children[0].lastmod == datetime(2026, 10, 14, tzinfo=UTC)
    assert parsed.children[1].lastmod is None
    assert parsed.entries == ()


def test_news_sitemap_traz_titulo_e_data_de_publicacao():
    parsed = parse_sitemap(NEWS)

    assert parsed.has_news is True
    entry = parsed.entries[0]
    assert entry.title == "Operação prende diretor"
    assert entry.publication_date == datetime(2026, 10, 14, 11, 0, tzinfo=UTC)


def test_aceita_conteudo_gzip():
    parsed = parse_sitemap(gzip.compress(URLSET))

    assert len(parsed.entries) == 3


def test_aceita_espacos_antes_da_declaracao_xml():
    parsed = parse_sitemap(b"\n\n  \r\n" + URLSET)

    assert len(parsed.entries) == 3


def test_aceita_bom_utf8_e_espacos_antes_da_declaracao():
    parsed = parse_sitemap(b"\xef\xbb\xbf\n " + URLSET)

    assert len(parsed.entries) == 3


def test_ignora_entradas_sem_loc_e_datas_invalidas():
    xml = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><lastmod>2026-10-14</lastmod></url>
      <url><loc>https://exemplo.com/x</loc><lastmod>ontem</lastmod></url>
    </urlset>"""

    parsed = parse_sitemap(xml)

    assert [e.loc for e in parsed.entries] == ["https://exemplo.com/x"]
    assert parsed.entries[0].lastmod is None


def test_funciona_sem_namespace_declarado():
    xml = b"<urlset><url><loc>https://exemplo.com/z</loc></url></urlset>"

    assert parse_sitemap(xml).entries[0].loc == "https://exemplo.com/z"


def test_xml_quebrado_levanta_erro():
    with pytest.raises(SitemapParseError, match="XML inválido"):
        parse_sitemap(b"<urlset><url><loc>https://exemplo.com/x</loc>")


def test_html_no_lugar_de_sitemap_levanta_erro():
    html = "<!doctype html><html><body>Página não encontrada</body></html>".encode()

    with pytest.raises(SitemapParseError):
        parse_sitemap(html)


def test_html_malformado_tem_mensagem_clara():
    html = b"<!DOCTYPE html><html><head><script async src=x></head><body><hr></body>"

    with pytest.raises(SitemapParseError, match="HTML"):
        parse_sitemap(html)


def test_raiz_desconhecida_levanta_erro():
    with pytest.raises(SitemapParseError, match="raiz"):
        parse_sitemap(b"<rss version='2.0'><channel/></rss>")


def test_nao_resolve_entidades_externas():
    xml = b"""<?xml version="1.0"?>
    <!DOCTYPE urlset [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <urlset><url><loc>https://exemplo.com/&xxe;</loc></url></urlset>"""

    try:
        parsed = parse_sitemap(xml)
    except SitemapParseError:
        return
    assert all("root:" not in e.loc for e in parsed.entries)


def test_gzip_acima_do_limite_descomprimido_e_rejeitado():
    bomba = gzip.compress(b"<urlset>" + b" " * 10_000 + b"</urlset>")

    with pytest.raises(SitemapParseError, match="limite"):
        parse_sitemap(bomba, max_bytes=1_000)


def test_xml_puro_acima_do_limite_e_rejeitado():
    with pytest.raises(SitemapParseError, match="limite"):
        parse_sitemap(URLSET, max_bytes=50)


def test_gzip_corrompido_levanta_erro():
    with pytest.raises(SitemapParseError, match="gzip"):
        parse_sitemap(b"\x1f\x8bnao-e-gzip-de-verdade")
