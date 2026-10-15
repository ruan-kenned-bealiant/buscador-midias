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


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "estado.db"
    store = StateStore(path)
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
    return path


@pytest.fixture
def base(db: Path) -> Iterator[str]:
    server = make_server(db, "127.0.0.1", 0, clock=lambda: NOW)
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


def test_banco_ausente_vira_503_sem_vazar_caminho(tmp_path: Path):
    server = make_server(tmp_path / "nao-existe.db", "127.0.0.1", 0, clock=lambda: NOW)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = get(f"http://127.0.0.1:{server.server_address[1]}/api/snapshot")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 503
    assert "nao-existe" not in body.decode()
    assert json.loads(body)["error"]


def test_o_servidor_nao_grava_no_estado(base: str, db: Path):
    before = db.stat().st_mtime_ns

    get(base + "/api/snapshot")

    store = StateStore(db, read_only=True)
    assert store.count_urls() == 1
    store.close()
    assert db.stat().st_mtime_ns == before


def test_pagina_nunca_injeta_dados_de_terceiros_como_html(base: str):
    _, _, body = get(base + "/")

    page = body.decode()
    assert "innerHTML" not in page
    assert "insertAdjacentHTML" not in page
    assert "document.write" not in page
    assert "eval(" not in page
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")  # sem recursos externos


def test_servidor_usa_o_arquivo_de_agenda_informado(db: Path, tmp_path: Path):
    agenda = tmp_path / "agenda.json"
    agenda.write_text(
        json.dumps(
            {"jobs": [{"name": "Rodada X", "kind": "discovery", "at": "2026-10-15T14:00:00+00:00"}]}
        ),
        encoding="utf-8",
    )
    server = make_server(db, "127.0.0.1", 0, clock=lambda: NOW, schedule_path=agenda)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, _, body = get(f"http://127.0.0.1:{server.server_address[1]}/api/snapshot")
    finally:
        server.shutdown()
        server.server_close()

    assert json.loads(body)["schedule"]["jobs"][0]["name"] == "Rodada X"


def test_cache_curto_evita_recalcular_o_snapshot_a_cada_requisicao(db: Path):
    now = [NOW]
    server = make_server(db, "127.0.0.1", 0, clock=lambda: now[0], cache_seconds=5)
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
