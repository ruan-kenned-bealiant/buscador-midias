import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from buscador_midias.dashboard.server import make_server
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import StateStore, UrlKind

NOW = datetime(2026, 10, 15, 12, 30, tzinfo=UTC)


def read_only() -> StateStore:
    return StateStore(read_only=True)


@pytest.fixture
def db() -> None:
    store = StateStore()
    run = store.start_run(NOW)
    store.record_urls(
        [
            DiscoveredUrl(
                url="https://a.com/pf-prende-empresario",
                url_hash="h1",
                site_key="a.com",
                source_sitemap="https://a.com/s.xml",
                lastmod=None,
                title="<script>alert(1)</script>",
                publication_date=None,
            )
        ],
        run_id=run,
        now=NOW,
        kind=UrlKind.NEW,
    )
    store.close()


@pytest.fixture
def base(db: None) -> Iterator[str]:
    server = make_server(read_only, "127.0.0.1", 0, clock=lambda: NOW)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def get(url: str) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - localhost
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def test_pagina_inicial_e_html_com_cabecalhos_de_seguranca(base: str):
    status, headers, body = get(base + "/")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert b"<title>" in body


def test_api_devolve_o_snapshot_em_json_sem_cache(base: str):
    status, headers, body = get(base + "/api/snapshot")

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == "no-store"
    data = json.loads(body)
    assert data["generated_at"] == NOW.isoformat()
    assert data["totals"]["by_kind"] == {"new": 1}
    assert data["recent"][0]["headline"] == "<script>alert(1)</script>"  # cru: a tela escapa


def test_parametros_hours_e_recent_sao_limitados(base: str):
    _, _, body = get(base + "/api/snapshot?hours=2&recent=1")
    data = json.loads(body)

    assert len(data["hourly"]) == 3
    assert len(data["recent"]) == 1

    _, _, body = get(base + "/api/snapshot?hours=99999&recent=abc")
    data = json.loads(body)
    assert len(data["hourly"]) == 169  # teto de 168 h
    assert len(data["recent"]) <= 40  # valor inválido cai no padrão


def test_rota_desconhecida_e_404(base: str):
    assert get(base + "/nada")[0] == 404


def test_metodos_que_nao_sao_get_sao_recusados(base: str):
    request = urllib.request.Request(base + "/api/snapshot", data=b"x", method="POST")

    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)  # noqa: S310 - localhost

    assert error.value.code == 405
    assert error.value.headers["Allow"] == "GET"


def test_banco_fora_do_ar_vira_503_sem_vazar_credenciais(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_MIDIAS_IA_PASSWORD", "segredo-123")
    monkeypatch.setenv("DB_MIDIAS_IA_PORT", "1")  # nada escuta nessa porta
    server = make_server(read_only, "127.0.0.1", 0, clock=lambda: NOW)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = get(f"http://127.0.0.1:{server.server_address[1]}/api/snapshot")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 503
    assert "segredo-123" not in body.decode()
    assert json.loads(body)["error"]


def test_o_servidor_nao_grava_no_estado(base: str):
    get(base + "/api/snapshot")

    store = StateStore(read_only=True)
    assert store.count_urls() == 1
    assert len(store.list_runs()) == 1
    store.close()


def test_pagina_nunca_injeta_dados_de_terceiros_como_html(base: str):
    _, _, body = get(base + "/")

    page = body.decode()
    assert "innerHTML" not in page
    assert "insertAdjacentHTML" not in page
    assert "document.write" not in page
    assert "eval(" not in page
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")  # sem recursos externos


def test_servidor_usa_o_arquivo_de_agenda_informado(db: None, tmp_path: Path):
    agenda = tmp_path / "agenda.json"
    agenda.write_text(
        json.dumps(
            {"jobs": [{"name": "Rodada X", "kind": "discovery", "at": "2026-10-15T14:00:00+00:00"}]}
        ),
        encoding="utf-8",
    )
    server = make_server(read_only, "127.0.0.1", 0, clock=lambda: NOW, schedule_path=agenda)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, _, body = get(f"http://127.0.0.1:{server.server_address[1]}/api/snapshot")
    finally:
        server.shutdown()
        server.server_close()

    assert json.loads(body)["schedule"]["jobs"][0]["name"] == "Rodada X"


def test_cache_curto_evita_recalcular_o_snapshot_a_cada_requisicao(db: None):
    now = [NOW]
    server = make_server(read_only, "127.0.0.1", 0, clock=lambda: now[0], cache_seconds=5)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        first = json.loads(get(base + "/api/snapshot")[2])
        now[0] = NOW + timedelta(seconds=3)
        within = json.loads(get(base + "/api/snapshot")[2])
        now[0] = NOW + timedelta(seconds=6)
        after = json.loads(get(base + "/api/snapshot")[2])
    finally:
        server.shutdown()
        server.server_close()

    assert within["generated_at"] == first["generated_at"]
    assert after["generated_at"] != first["generated_at"]


def test_cache_nao_mistura_parametros_diferentes(base: str):
    a = json.loads(get(base + "/api/snapshot?hours=2")[2])
    b = json.loads(get(base + "/api/snapshot?hours=4")[2])

    assert len(a["hourly"]) == 3 and len(b["hourly"]) == 5


@pytest.mark.parametrize("probe_kind", ["prioritarios", "feeder", "feeder-pendentes", "cobertura"])
def test_evidencia_do_teste_prioritario_reutiliza_visualizador_sem_acesso_ao_corpus(
    tmp_path: Path,
    probe_kind: str,
):
    probe = tmp_path / "probe"
    folder = probe / "evidence/midias-ia/evidencias"
    folder.mkdir(parents=True)
    digest = "a" * 64
    original = b"<!doctype html><html><body>Materia de teste</body></html>"
    (folder / f"{digest}.html").write_bytes(original)
    (probe / "report.html").write_text("<h1>Teste de noticias</h1>")
    server = make_server(
        read_only,
        "127.0.0.1",
        0,
        manager_db_path=tmp_path / "catalog.sqlite",
        priority_probe_directory=probe,
        feeder_mapping_directory=probe,
        feeder_pending_directory=probe,
        coverage_directory=probe,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert get(base + "/testes/" + probe_kind)[0] == 200
        if probe_kind == "cobertura":
            return  # Este relatório não expõe rotas de captura de evidências.
        evidence_prefix = "teste-prioritarios" if probe_kind == "prioritarios" else probe_kind
        url = base + "/evidencias/" + evidence_prefix + "/" + digest
        status, headers, body = get(url)
        assert status == 200
        assert b"/assets/evidence.js" in body
        assert b"Materia de teste" not in body
        status, headers, body = get(url + "?original=1")
        assert status == 200 and body == original
        assert "sandbox" in headers["Content-Security-Policy"]
        assert "script-src 'none'" in headers["Content-Security-Policy"]
        assert get(base + "/evidencias/teste-prioritarios/" + "b" * 64)[0] == 404
        assert get(base + "/evidencias/teste-prioritarios/../secret")[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_folha_e_script_compartilhados_sao_servidos_com_cabecalhos_seguros(base: str):
    status, headers, body = get(base + "/assets/app.css")

    assert status == 200
    assert headers["Content-Type"].startswith("text/css")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "style-src 'self'" in headers["Content-Security-Policy"]
    status, headers, _ = get(base + "/assets/app.js")
    assert status == 200
    assert headers["Content-Type"].startswith("application/javascript")


@pytest.mark.parametrize("path", ["/", "/saude", "/mecanismos", "/buscador", "/guia"])
def test_todas_as_telas_usam_a_mesma_folha_e_o_mesmo_script(base: str, path: str):
    _, _, body = get(base + path)

    assert b'href="/assets/app.css"' in body
    assert b'src="/assets/app.js"' in body


def test_folha_e_sempre_clara_e_cobre_movimento_reduzido_e_carregamento(base: str):
    _, _, body = get(base + "/assets/app.css")
    css = body.decode()

    # O tema do sistema não muda o painel: o escuro só existe sob data-theme="dark".
    assert "prefers-color-scheme" not in css
    assert "color-scheme: light" in css and ':root[data-theme="dark"]' in css
    assert "prefers-reduced-motion" in css
    assert ".skeleton" in css
    assert "button.is-busy" in css
    assert ":focus-visible" in css


def test_script_compartilhado_expoe_ajudantes_de_carregamento(base: str):
    _, _, body = get(base + "/assets/app.js")
    script = body.decode()

    for name in ("busy", "skeleton", "track"):
        assert f"{name}(" in script
    assert "window.UI" in script


def test_a_pagina_inicial_e_o_painel_e_o_gerenciador_de_sites_fica_em_gerenciador(base: str):
    _, _, home = get(base + "/")
    _, _, sites = get(base + "/gerenciador")

    assert b'id="panel"' in home and b'id="sites-view"' not in home
    assert b'id="sites-view"' in sites and b'id="panel"' not in sites
    status, headers, script = get(base + "/assets/panel.js")
    assert status == 200 and headers["Content-Type"].startswith("application/javascript")
    assert b"/api/gerenciador/busca/painel" in script


@pytest.mark.parametrize(
    "path", ["/", "/gerenciador", "/saude", "/mecanismos", "/buscador", "/guia"]
)
def test_o_menu_de_todas_as_telas_leva_ao_painel_e_ao_gerenciador(base: str, path: str):
    _, _, body = get(base + path)

    assert b'<a href="/"' in body or b'href="/" ' in body or b'href="/"' in body
    assert b'href="/gerenciador"' in body
    assert body.count(b">Painel<") + body.count(b"Painel</a>") >= 1
