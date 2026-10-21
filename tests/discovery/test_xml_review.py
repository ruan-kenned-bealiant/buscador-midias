import gzip

import pytest

from buscador_midias.discovery.sitemap_parser import SitemapParseError
from buscador_midias.discovery.xml_review import review_xml, transient
from buscador_midias.net.fetcher import FetchResult


def test_indice_apenas_devolve_referencias_inclusive_ciclo_sem_expandir():
    result = review_xml(
        b"<sitemapindex><sitemap><loc>https://x.com/sitemap.xml</loc></sitemap></sitemapindex>",
        "https://x.com/sitemap.xml",
    )
    assert result.children == ("https://x.com/sitemap.xml",)
    assert result.entries == ()


def test_feed_antigo_continua_elegivel_e_audio_tem_evidencia_estrutural():
    result = review_xml(
        b"<rss><channel><item><link>https://x.com/noticia/antiga</link>"
        b"<pubDate>Mon, 01 Jan 2001 00:00:00 GMT</pubDate>"
        b'<enclosure type="audio/mpeg" url="https://x.com/audio.mp3"/>'
        b"</item></channel></rss>",
        "https://x.com/feed",
    )
    assert result.entries == ("https://x.com/noticia/antiga",)
    assert result.podcast


def test_html_nao_vira_xml_funcional():
    with pytest.raises(SitemapParseError):
        review_xml(b"<html><title>Bloqueado</title></html>", "https://x.com/feed")


def test_403_e_dns_permanente_nao_disparam_repeticao():
    assert not transient(FetchResult("https://x.com", 403, b"", None))
    assert not transient(FetchResult("https://x.com", None, b"", "Name or service not known"))
    assert transient(FetchResult("https://x.com", 503, b"", None))
    assert transient(FetchResult("https://x.com", None, b"", "ReadTimeout"))


def test_sitemap_gzip_e_interpretado_sem_tentar_decodificar_como_feed():
    body = gzip.compress(
        b"<sitemapindex><sitemap><loc>https://x.com/child.xml</loc></sitemap></sitemapindex>"
    )
    result = review_xml(body, "https://x.com/index.xml.gz")
    assert result.kind == "sitemap" and result.children == ("https://x.com/child.xml",)
