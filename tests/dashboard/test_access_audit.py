import csv
import hashlib
import json
import threading

from tests.dashboard.test_server import get

from buscador_midias.dashboard.access_audit import AccessAudit
from buscador_midias.dashboard.server import make_server
from buscador_midias.domain.site import Site
from buscador_midias.strategy.manual import ManualRules, ManualRulesBook


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def index(directory, rules=None):
    sites = [
        Site.create(
            name=host,
            url="https://" + host,
            region="",
            country="Brasil",
            country_code="BR",
            language="pt",
            source_type="portal",
        )
        for host in ("a.test", "b.test")
    ]
    return AccessAudit(directory, sites, rules or ManualRulesBook({}))


def reports(directory):
    write_csv(
        directory / "auditoria-acesso-sitemaps/referencias.csv",
        [
            {
                "site_key": "a.test",
                "root": "https://a.test/sitemap.xml",
                "status": "readable_news",
                "accessible": "True",
                "readable": "True",
                "xml_readable": "True",
                "example_url": "https://a.test/antiga-de-2001",
                "example_http": "200",
            }
        ],
    )
    write_csv(
        directory / "auditoria-acesso-rss/feeds.csv",
        [
            {
                "site_key": "a.test",
                "feed": "https://a.test/rss",
                "tentado": "True",
                "status": "article_page_only",
                "noticia_acessivel": "True",
                "texto_legivel": "False",
                "exemplo": "https://a.test/noticia",
                "motivos_amostras": "paywall",
            }
        ],
    )
    write_csv(
        directory / "auditoria-acesso-rss/inventario.csv",
        [
            {
                "site_key": "a.test",
                "feed": "https://a.test/rss",
                "tentado": "True",
                "status_auditoria": "article_page_only",
            },
            {
                "site_key": "b.test",
                "feed": "https://b.test/rss",
                "tentado": "False",
                "status_auditoria": "nao_testado_limite_3_feeds",
            },
        ],
    )


def test_uniao_deduplica_fonte_e_nao_confunde_xml_com_acesso(tmp_path):
    reports(tmp_path)
    audit = index(tmp_path)
    audit.refresh()
    assert audit.summary["sources_accessible"] == 1
    assert audit.summary["sources_readable"] == 1
    assert audit.summary["rss_sources_accessible"] == 1
    assert audit.summary["sitemap_sources_accessible"] == 1
    assert not audit.sources["b.test"]["audited"]
    untested = audit.page("b.test", kind="feed", mode="untested", search="", offset=0, limit=10)
    assert untested["total"] == 1 and not untested["records"][0]["accessible"]
    readable = audit.page("a.test", kind="all", mode="readable", search="2001", offset=0, limit=10)
    assert readable["total"] == 1  # notícias antigas continuam na evidência de acesso
    readable = audit.page("a.test", kind="all", mode="readable", search="", offset=0, limit=10)
    assert readable["total"] == 1
    assert readable["records"][0]["example_url"].endswith("antiga-de-2001")
    assert (
        audit.page("a.test", kind="feed", mode="readable", search="", offset=0, limit=10)["total"]
        == 0
    )
    assert (
        audit.page("a.test", kind="all", mode="all", search="", offset=99, limit=10)["records"]
        == []
    )


def test_nao_expoe_links_estranhos_e_respeita_exclusoes(tmp_path):
    write_csv(
        tmp_path / "auditoria-acesso-sitemaps/referencias.csv",
        [
            {
                "site_key": "a.test",
                "root": "https://a.test/s.xml",
                "accessible": "True",
                "readable": "True",
                "example_url": "javascript:alert(1)",
            },
            {
                "site_key": "a.test",
                "root": "https://outro.test/s.xml",
                "accessible": "True",
                "readable": "True",
                "example_url": "https://a.test/noticia",
            },
            {
                "site_key": "a.test",
                "root": "https://a.test/excluido.xml",
                "accessible": "True",
                "readable": "True",
                "example_url": "https://a.test/noticia",
            },
        ],
    )
    audit = index(
        tmp_path, ManualRulesBook({"a.test": ManualRules(("https://a.test/excluido.xml",), ())})
    )
    audit.refresh()
    page = audit.page("a.test", kind="all", mode="all", search="", offset=0, limit=10)
    assert page["total"] == 1 and page["records"][0]["example_url"] is None
    assert audit.summary["sources_accessible"] == 0


def test_ausencia_de_arquivos_e_releitura_de_exportacao_nova(tmp_path):
    audit = index(tmp_path)
    audit.refresh()
    assert not audit.summary["available"]
    reports(tmp_path)
    audit.refresh()
    assert audit.summary["available"] and audit.summary["sources_accessible"] == 1
    original = audit.records
    audit.refresh()
    assert audit.records is original


def test_endpoint_de_auditoria_nao_abre_banco_e_valida_parametros(tmp_path):
    reports(tmp_path)
    sites = tmp_path / "sites.csv"
    write_csv(
        sites,
        [
            {
                "region": "",
                "name": "A",
                "url": "https://a.test",
                "country": "Brasil",
                "country_code": "BR",
                "language": "pt",
                "source_type": "portal",
            }
        ],
    )

    def forbidden_store():
        raise AssertionError("a auditoria deve consultar apenas os arquivos")

    server = make_server(
        forbidden_store, "127.0.0.1", 0, sites_path=sites, audit_directory=tmp_path
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, _, body = get(base + "/api/mapa/acesso?site=a.test&mode=readable&limit=1")
        assert status == 200 and json.loads(body)["total"] == 1
        assert get(base + "/api/mapa/acesso?site=outro")[0] == 404
        assert json.loads(get(base + "/api/mapa/revisao-web?site=a.test")[2]) == {
            "available": False
        }
        web_dir = tmp_path / "revisao-web-fontes/detalhes"
        web_dir.mkdir(parents=True)
        report_id = hashlib.sha256(b"a.test").hexdigest()
        (web_dir / f"{report_id}.json").write_text(
            json.dumps({"available": True, "queries": ["site:a.test RSS"]})
        )
        assert json.loads(get(base + "/api/mapa/revisao-web?site=a.test")[2])["queries"] == [
            "site:a.test RSS"
        ]
        assert get(base + "/api/mapa/revisao-web?site=../../etc/passwd")[0] == 404
        assert get(base + "/api/mapa/acesso?site=a.test&kind=invalido")[0] == 400
        assert get(base + "/api/mapa/acesso?site=a.test&mode=invalido")[0] == 400
        assert get(base + "/api/mapa/auditoria.xlsx")[0] == 404
        report = tmp_path / "auditoria-acesso-rss/resultado.xlsx"
        report.write_bytes(b"xlsx-local")
        assert get(base + "/api/mapa/auditoria.xlsx")[2] == b"xlsx-local"
        assert get(base + "/api/mapa/auditoria.xlsx?path=/etc/passwd")[2] == b"xlsx-local"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_exploracao_bloqueada_nao_vira_xml_funcional_ou_noticia_confirmada(tmp_path):
    reports(tmp_path)
    write_csv(
        tmp_path / "exploracao-pendentes-final/fontes.csv",
        [{"site_key": "b.test", "status": "mechanism_known_blocked", "detail": "Robots: 403."}],
    )
    write_csv(
        tmp_path / "exploracao-pendentes-final/acesso.csv",
        [
            {
                "site_key": "b.test",
                "root": "https://b.test/sitemap-news.xml",
                "kind": "sitemap",
                "attempted": "False",
                "status": "mechanism_known_blocked",
                "xml_readable": "False",
                "accessible": "False",
                "readable": "False",
                "basis": "official_robots_external",
            }
        ],
    )
    audit = index(tmp_path)
    audit.refresh()
    source = audit.sources["b.test"]
    assert source["explored"] and not source["audited"] and not source["accessible"]
    assert source["exploration_status"] == "mechanism_known_blocked"
    assert audit.summary["sources_accessible"] == 1
    record = audit.page("b.test", kind="sitemap", mode="untested", search="", offset=0, limit=10)[
        "records"
    ][0]
    assert record["basis"] == "official_robots_external" and record["xml_readable"] is None


def test_exploracao_acrescenta_fonte_com_noticia_real(tmp_path):
    reports(tmp_path)
    write_csv(
        tmp_path / "exploracao-pendentes-final/acesso.csv",
        [
            {
                "site_key": "b.test",
                "root": "https://b.test/feed",
                "kind": "feed",
                "attempted": "True",
                "status": "readable_news",
                "xml_readable": "True",
                "accessible": "True",
                "readable": "True",
                "example_url": "https://b.test/noticia-antiga",
                "example_http": "200",
            }
        ],
    )
    audit = index(tmp_path)
    audit.refresh()
    assert audit.summary["sources_accessible"] == 2
    assert audit.summary["rss_sources_readable"] == 1
    assert audit.summary["sources_readable"] == 2


def test_nova_leitura_de_xml_nao_apaga_noticia_ja_confirmada(tmp_path):
    reports(tmp_path)
    write_csv(
        tmp_path / "exploracao-pendentes-final/acesso.csv",
        [
            {
                "site_key": "a.test",
                "root": "https://a.test/rss",
                "kind": "feed",
                "attempted": "True",
                "status": "xml_confirmed_no_news",
                "xml_readable": "True",
                "accessible": "False",
                "readable": "False",
            }
        ],
    )
    audit = index(tmp_path)
    audit.refresh()
    page = audit.page("a.test", kind="feed", mode="all", search="", offset=0, limit=10)
    assert page["total"] == 1 and page["records"][0]["accessible"]
    assert page["records"][0]["example_url"] == "https://a.test/noticia"


def test_revisao_web_integra_novos_mecanismos_e_preserva_origem_oficial(tmp_path):
    reports(tmp_path)
    write_csv(
        tmp_path / "revisao-web-fontes/acesso.csv",
        [
            {
                "site_key": "b.test",
                "root": "https://b.test/politica/feed",
                "kind": "feed",
                "attempted": "True",
                "xml_readable": "True",
                "accessible": "True",
                "readable": "True",
                "status": "readable_news",
                "example_url": "https://b.test/politica/noticia",
                "basis": "official_directory",
                "found_on": "https://b.test/siga-por-rss.html",
            }
        ],
    )
    write_csv(
        tmp_path / "revisao-web-fontes/fontes.csv",
        [
            {
                "site_key": "b.test",
                "status": "new_xml",
                "queries": "2",
                "pages_read": "2",
                "new_xml": "1",
            }
        ],
    )
    audit = index(tmp_path)
    audit.refresh()
    assert audit.summary["sources_accessible"] == 2
    assert audit.sources["b.test"]["web_review"]["status"] == "new_xml"
    row = audit.page("b.test", kind="feed", mode="readable", search="", offset=0, limit=10)[
        "records"
    ][0]
    assert row["found_on"] == "https://b.test/siga-por-rss.html"


def final_reports(directory):
    write_csv(
        directory / "validacao-final/acesso.csv",
        [
            {
                "site_key": "a.test",
                "root": "https://a.test/sitemap.xml",
                "kind": "sitemap",
                "attempted": "True",
                "xml_readable": "False",
                "accessible": "False",
                "readable": "False",
                "status": "mechanism_unreadable",
                "example_url": "",
                "traversal_partial": "False",
                "samples": "[]",
            },
            {
                "site_key": "b.test",
                "root": "https://b.test/rss",
                "kind": "feed",
                "attempted": "True",
                "xml_readable": "True",
                "accessible": "True",
                "readable": "True",
                "status": "readable_news",
                "example_url": "https://b.test/antiga",
                "traversal_partial": "True",
                "samples": json.dumps(
                    [
                        {"url": "https://b.test/antiga", "readable": True, "accessible": True},
                        {"url": "javascript:alert(1)", "accessible": True},
                        {"url": "https://outro.test/noticia", "accessible": True},
                    ]
                ),
            },
        ],
    )
    write_csv(
        directory / "validacao-final/fontes.csv",
        [{"site_key": "a.test", "tested": "1"}, {"site_key": "b.test", "tested": "1"}],
    )
    write_csv(
        directory / "validacao-final/secoes.csv",
        [
            {
                "site_key": "b.test",
                "url": "https://b.test/politica",
                "title": "Política",
                "accessible": "True",
                "example_url": "https://b.test/politica/antiga",
            },
            {
                "site_key": "b.test",
                "url": "https://b.test/cultura",
                "title": "Cultura",
                "accessible": "False",
                "example_url": "https://outro.test/noticia",
            },
        ],
    )
    (directory / "validacao-final/resumo.json").write_text(json.dumps({"closed": True}))


def test_validacao_final_substitui_retrato_antigo_e_preserva_limites(tmp_path):
    reports(tmp_path)
    final_reports(tmp_path)
    audit = index(tmp_path)
    audit.refresh()
    assert not audit.sources["a.test"]["accessible"]  # antes havia evidência positiva
    assert audit.sources["a.test"]["mechanisms"] == 1  # universo congelado desta rodada
    assert audit.sources["b.test"]["readable"]
    assert audit.summary["final_validation"]["closed"]
    page = audit.page("b.test", kind="all", mode="partial", search="", offset=0, limit=10)
    assert page["total"] == 1
    assert page["records"][0]["partial"] and page["records"][0]["readable"]
    assert len(page["records"][0]["samples"]) == 1  # descarta links inseguros/externos
    assert (
        audit.sections_page("b.test", mode="confirmed", search="pol", offset=0, limit=10)["total"]
        == 1
    )
    assert (
        audit.sections_page("b.test", mode="unconfirmed", search="", offset=0, limit=10)[
            "sections"
        ][0]["example_url"]
        == ""
    )


def test_relatorio_final_em_preparacao_nao_apaga_evidencia_anterior(tmp_path):
    reports(tmp_path)
    final_reports(tmp_path)
    path = tmp_path / "validacao-final/resumo.json"
    path.write_text(json.dumps({"closed": False}))
    audit = index(tmp_path)
    audit.refresh()
    assert audit.sources["a.test"]["readable"]
    path.write_text(json.dumps({"closed": True}))
    audit.refresh()
    assert not audit.sources["a.test"]["readable"]


def test_validacao_por_secao_e_download_final_sem_banco(tmp_path):
    final_reports(tmp_path)
    sites = tmp_path / "sites.csv"
    write_csv(
        sites,
        [
            {
                "region": "",
                "name": "B",
                "url": "https://b.test",
                "country": "Brasil",
                "country_code": "BR",
                "language": "pt",
                "source_type": "portal",
            }
        ],
    )

    def forbidden_store():
        raise AssertionError("validação final usa somente arquivos locais")

    server = make_server(
        forbidden_store, "127.0.0.1", 0, sites_path=sites, audit_directory=tmp_path
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        path = "/api/mapa/validacao-secoes?site=b.test"
        status, _, body = get(base + path + "&mode=confirmed")
        assert status == 200 and json.loads(body)["total"] == 1
        assert json.loads(body)["sections"][0]["title"] == "Política"
        assert get(base + path + "&mode=bad")[0] == 400
        assert get(base + "/api/mapa/validacao-secoes?site=unknown")[0] == 404
        status, _, body = get(base + "/api/mapa/acesso?site=b.test&mode=partial")
        assert status == 200 and json.loads(body)["total"] == 1
        assert json.loads(body)["summary"]["available"]
        (tmp_path / "validacao-final/resultado.xlsx").write_bytes(b"final-local")
        assert get(base + "/api/mapa/auditoria.xlsx")[2] == b"final-local"
        assert get(base + "/api/mapa/plano-validado.json")[0] == 404
        (tmp_path / "validacao-final/plano-com-noticias.json").write_text("[]")
        assert get(base + "/api/mapa/plano-validado.json?path=/etc/passwd")[2] == b"[]"
        assert get(base + "/api/mapa/plano-validado.json?mode=bad")[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
