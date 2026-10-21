"""Contagens sem corte de data e consulta funcional sem banco ou coleta."""

import gzip
import json
import threading
import urllib.error
import urllib.request

import pytest

from buscador_midias.dashboard.functional import count_xml
from buscador_midias.dashboard.server import make_server


def test_rss_antigo_e_atom_contam_urls_distintas_sem_corte_de_data():
    rss = b"""<rss><channel>
    <item><link>https://example.com/antiga</link>
    <pubDate>Mon, 01 Jan 2001 12:00:00 GMT</pubDate></item>
    <item><link>https://example.com/antiga</link></item>
    <item><link>https://example.com/sem-data</link></item></channel></rss>"""
    assert count_xml(rss, "https://example.com/feed/") == dict(
        kind="feed", listed=2, children=0, index=False
    )
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <link href="https://example.com/antiga"/><updated>2001-01-01T12:00:00Z</updated>
    </entry></feed>"""
    assert count_xml(atom, "https://example.com/atom.xml")["listed"] == 1


def test_indice_comprimido_nao_conta_filhos_como_noticias():
    body = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <sitemap><loc>https://example.com/2001.xml</loc></sitemap>
    <sitemap><loc>https://example.com/2002.xml</loc></sitemap></sitemapindex>"""
    expected = dict(kind="sitemap", listed=0, children=2, index=True)
    assert count_xml(body, "https://example.com/index.xml") == expected
    assert count_xml(gzip.compress(body), "https://example.com/index.xml.gz") == expected
    assert (
        count_xml(
            b"<urlset><url><loc>https://example.com/a</loc>"
            b"<lastmod>2001-01-01</lastmod></url></urlset>",
            "https://example.com/s.xml",
        )["listed"]
        == 1
    )
    with pytest.raises(ValueError):
        count_xml(b"<html><body>Blocked</body></html>", "https://example.com/sitemap.xml")


@pytest.fixture
def functional_server(tmp_path):
    source = dict(
        key="example.com",
        name="<script>Fonte</script>",
        country="Brasil",
        functional=2,
        rss=1,
        sitemaps=1,
        round_collected=14,
        round_attributed=0,
        round_unattributed=14,
    )
    records = [
        dict(
            url="https://example.com/feed/",
            kind="feed",
            listed=14,
            children=0,
            index=False,
            round_news=None,
            texts=0,
            article_blocked=True,
        ),
        dict(
            url="https://example.com/index.xml",
            kind="sitemap",
            listed=0,
            children=385,
            index=True,
            round_news=0,
            texts=0,
            article_blocked=False,
        ),
    ]
    (tmp_path / "retrato.json").write_text(
        json.dumps(
            dict(
                meta=dict(sources=1, sources_functional=1),
                sources=[source],
                mechanisms={"example.com": records},
            )
        )
    )
    (tmp_path / "funcionais.csv").write_text("url,tipo\nhttps://example.com/feed/,feed\n")

    def forbidden():
        raise AssertionError("A consulta funcional não abre banco nem faz coleta")

    server = make_server(forbidden, "127.0.0.1", 0, functional_directory=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", tmp_path
    server.shutdown()
    server.server_close()
    thread.join(5)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as response:
        return response.code, response.read()


def test_consulta_xml_funcional_independe_de_acesso_ao_artigo(functional_server):
    base, _ = functional_server
    for path in ("/funcionais", "/selecao", "/classificacao"):
        assert get(base + path)[0] == 200
    code, body = get(base + "/api/funcionais")
    assert code == 200 and json.loads(body)["sources"][0]["functional"] == 2
    _, body = get(base + "/api/funcionais/mecanismos?site=example.com&tipo=feed")
    rss = json.loads(body)["items"][0]
    assert rss["article_blocked"] and rss["listed"] == 14 and rss["round_news"] is None
    _, body = get(base + "/api/funcionais/mecanismos?site=example.com&offset=1&limit=1")
    index = json.loads(body)["items"][0]
    assert index["listed"] == 0 and index["children"] == 385
    _, body = get(base + "/api/funcionais/mecanismos?site=example.com&q=index")
    assert json.loads(body)["total"] == 1
    assert get(base + "/api/funcionais/arquivo?nome=funcionais.csv")[0] == 200
    assert get(base + "/api/funcionais/arquivo?nome=../.env")[0] == 400
    for query in ("offset=-1", "limit=101", "limit=x", "tipo=bootstrap"):
        assert get(base + "/api/funcionais/mecanismos?site=example.com&" + query)[0] == 400
    assert get(base + "/api/funcionais/mecanismos?site=unknown")[0] == 404


def test_recarrega_retrato_e_reporta_ausencia(functional_server):
    base, folder = functional_server
    path = folder / "retrato.json"
    data = json.loads(path.read_text())
    data["sources"][0]["functional"] = 3
    path.write_text(json.dumps(data))
    assert json.loads(get(base + "/api/funcionais")[1])["sources"][0]["functional"] == 3
    path.unlink()
    assert get(base + "/api/funcionais")[0] == 503


def test_filtro_de_texto_nao_inclui_canal_de_audio(functional_server):
    base, folder = functional_server
    path = folder / "retrato.json"
    snapshot = json.loads(path.read_text())
    snapshot["mechanisms"]["example.com"] += [
        dict(url="https://example.com/articles.xml", kind="sitemap", texts=2, audio=False),
        dict(url="https://example.com/podcast.xml", kind="feed", texts=2, audio=True),
    ]
    path.write_text(json.dumps(snapshot))
    _, body = get(base + "/api/funcionais/mecanismos?site=example.com&uso=text")
    assert [r["url"] for r in json.loads(body)["items"]] == ["https://example.com/articles.xml"]
    _, body = get(base + "/api/funcionais/mecanismos?site=example.com&uso=audio")
    assert [r["url"] for r in json.loads(body)["items"]] == ["https://example.com/podcast.xml"]
    _, body = get(base + "/api/funcionais/mecanismos?site=example.com&uso=xml")
    assert json.loads(body)["total"] == 2
    assert get(base + "/api/funcionais/mecanismos?site=example.com&uso=invalid")[0] == 400
