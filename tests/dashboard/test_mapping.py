import csv
import json
import threading
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from tests.dashboard.test_server import get, read_only

from buscador_midias.dashboard.server import make_server
from buscador_midias.domain.mapping import MapPage, PageStatus
from buscador_midias.state.store import StateStore

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


@pytest.fixture
def map_base(tmp_path):
    catalog = tmp_path / "sites.csv"
    with catalog.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ("region", "name", "url", "country", "country_code", "language", "source_type")
        )
        for name, url, country in [
            ("Fonte A", "https://a.com", "Brasil"),
            ("Fonte B", "https://b.com", "Portugal"),
            ("Alias A", "https://www.a.com", "Brasil"),
        ]:
            writer.writerow(("", name, url, country, "", "pt", "portal"))
    store = StateStore()
    store.save_mapping_pages(
        "a.com",
        [
            MapPage("https://a.com", "", "root", PageStatus.OK, checked_at=NOW),
            MapPage(
                "https://a.com/politica",
                "https://a.com",
                "section",
                PageStatus.OK,
                title="Política <script>alert(1)</script>",
                page_type="section",
                discovery_basis="navigation",
                checked_at=NOW,
                article_urls=("javascript:alert(1)", "https://a.com/noticia-de-exemplo"),
            ),
            MapPage("https://a.com/economia", "", "section", PageStatus.OK),
            MapPage("https://a.com/pendente", "", "section"),
            MapPage("https://a.com/quebrada", "", "section", PageStatus.ERROR, detail="HTTP 404"),
            MapPage("https://a.com/login", "", "section", PageStatus.EXCLUDED, page_type="service"),
        ],
    )
    store.close()
    server = make_server(read_only, "127.0.0.1", 0, sites_path=catalog)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_catalogo_inclui_fontes_sem_mapa_e_identifica_duplicatas(map_base):
    status, _, body = get(map_base + "/api/mapa/fontes")
    data = json.loads(body)
    assert status == 200
    assert (data["records"], data["unique_sources"], data["duplicate_records"]) == (3, 2, 1)
    assert data["sites"][1]["key"] == "b.com" and data["sites"][1]["read"] == 0
    assert data["sites"][2]["duplicate"]
    assert data["totals"]["confirmed_sections"] == 1
    assert data["totals"]["with_examples"] == 1


def test_secao_traz_exemplo_seguro_e_data_sem_injetar_titulo(map_base):
    data = json.loads(get(map_base + "/api/mapa/secoes?site=a.com")[2])
    assert data["total"] == 1
    section = data["sections"][0]
    assert section["example_url"] == "https://a.com/noticia-de-exemplo"
    assert section["checked_at"] == NOW.isoformat()
    assert "<script>" in section["title"]  # texto cru; o DOM usa nós de texto
    assert "samples" not in section


def test_paginacao_busca_e_lacunas_nao_omitem_dados(map_base):
    route = map_base + "/api/mapa/secoes?"
    all_rows = json.loads(get(route + "site=a.com&mode=all&limit=1&offset=1")[2])
    assert all_rows["total"] == 5 and len(all_rows["sections"]) == 1
    end = json.loads(get(route + "site=a.com&mode=all&offset=99")[2])
    assert end["total"] == 5 and end["sections"] == []
    gaps = json.loads(get(route + "site=a.com&mode=gaps")[2])
    assert {s["status"] for s in gaps["sections"]} == {"pending", "error"}
    params = urlencode({"site": "a.com", "mode": "read", "q": "Política"})
    search = json.loads(get(route + params)[2])
    assert search["total"] == 1
    literal = json.loads(get(route + urlencode({"site": "a.com", "q": "%_' OR 1=1"}))[2])
    assert literal["total"] == 0


def test_fonte_sem_secoes_e_fonte_desconhecida_sao_distintas(map_base):
    assert json.loads(get(map_base + "/api/mapa/secoes?site=b.com")[2])["total"] == 0
    assert get(map_base + "/api/mapa/secoes?site=inexistente")[0] == 404
    assert get(map_base + "/api/mapa/secoes?site=a.com&mode=invalido")[0] == 400


def test_tela_mapa_e_painel_anterior_permanecem_acessiveis(map_base):
    assert "Mapa de fontes" in get(map_base + "/")[2].decode()
    assert "Mapa de fontes" in get(map_base + "/mapa")[2].decode()
    assert "painel do fluxo" in get(map_base + "/fluxo")[2].decode()
    store = StateStore(read_only=True)
    assert len(store.mapping_pages("a.com")) == 6
    assert store.list_runs() == []
    store.close()


def test_mecanismos_trazem_urls_tipos_datas_e_csv_consumivel(map_base):
    from buscador_midias.domain.mapping import MapSource

    store = StateStore()
    store.save_mapping_sources(
        "a.com",
        [
            MapSource(
                "https://a.com/rss", "fresh", "feed", ("https://a.com/noticia-de-exemplo",), NOW
            ),
            MapSource(
                "https://a.com/sitemap-2026-10-18.xml",
                "fresh",
                "sitemap",
                ("https://a.com/noticia-de-exemplo",),
                NOW,
            ),
            MapSource("https://a.com/vazio.xml", "empty", "feed", (), NOW),
            MapSource("https://a.com/lista.html", "invalid", "html_pagination", (), NOW),
        ],
    )
    store.close()
    data = json.loads(get(map_base + "/api/mapa/mecanismos?site=a.com")[2])
    assert data["total"] == 1
    assert data["mechanisms"][0]["url"] == "https://a.com/rss"
    assert data["mechanisms"][0]["checked_at"] == NOW.isoformat()
    review = json.loads(get(map_base + "/api/mapa/mecanismos?site=a.com&mode=review")[2])
    assert review["total"] == 2
    assert get(map_base + "/api/mapa/mecanismos?site=desconhecido")[0] == 404
    assert get(map_base + "/api/mapa/mecanismos?mode=invalido")[0] == 400
    status, headers, body = get(map_base + "/api/mapa/mecanismos.csv?site=a.com")
    assert status == 200 and headers["Content-Type"].startswith("text/csv")
    import io

    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    assert rows[0]["rss_url"] == "https://a.com/rss"
    section = json.loads(get(map_base + "/api/mapa/secoes?site=a.com")[2])["sections"][0]
    assert section["coverage"]["status"] == "sample_full"
    assert section["coverage"]["mechanisms"][0]["url"] == "https://a.com/rss"
    catalog = json.loads(get(map_base + "/api/mapa/fontes")[2])
    assert catalog["totals"]["with_recent_mechanisms"] == 1
    assert catalog["totals"]["recent_mechanisms"] == 1
    assert catalog["totals"]["with_usable_mechanisms"] == 1
    assert catalog["totals"]["without_recent_mechanisms"] == 1
