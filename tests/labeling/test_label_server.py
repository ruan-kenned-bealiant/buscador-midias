import csv
import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_labeling import build_store  # fixtures do módulo irmão

from buscador_midias.labeling.export import export_batch
from buscador_midias.labeling.server import make_label_server


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    export_batch(build_store(findings=2, no_entity=2), tmp_path, per_group=2, seed=1)
    return tmp_path


@pytest.fixture
def port(folder: Path) -> Iterator[int]:
    server = make_label_server(folder, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def call(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def vote(port: int, label_id: str, verdict: str, **extra: str) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json", **extra}
    payload = json.dumps({"id": label_id, "verdict": verdict}).encode()
    return call(port, "POST", "/api/verdict", payload, headers)


def saved(folder: Path) -> list[dict[str, str]]:
    with (folder / "rotulagem-01.csv").open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_pagina_sai_com_cabecalhos_de_seguranca(port: int):
    status, headers, body = call(port, "GET", "/")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert b"corpus" in body.lower()


def test_itens_trazem_so_o_que_e_cego(port: int):
    status, _, body = call(port, "GET", "/api/items")

    data = json.loads(body)
    assert status == 200
    assert data["total"] == 4 and data["done"] == 0
    assert set(data["items"][0]) == {"id", "source", "url", "headline", "excerpt", "verdict"}
    text = body.decode()
    assert "no_entity" not in text and "in_corpus" not in text
    assert "João Silva" not in text  # entidade do modelo fica na chave


def test_voto_grava_no_csv_e_atualiza_o_progresso(port: int, folder: Path):
    first = json.loads(call(port, "GET", "/api/items")[2])["items"][0]["id"]

    status, _, body = vote(port, first, "nao")

    assert status == 200
    assert json.loads(body) == {"ok": True, "done": 1, "total": 4}
    assert saved(folder)[0]["veredito"] == "nao"
    assert json.loads(call(port, "GET", "/api/items")[2])["done"] == 1


def test_voto_vazio_desfaz(port: int, folder: Path):
    first = json.loads(call(port, "GET", "/api/items")[2])["items"][0]["id"]
    vote(port, first, "sim")

    assert vote(port, first, "")[0] == 200

    assert saved(folder)[0]["veredito"] == ""


@pytest.mark.parametrize("verdict", ["adverso", "SIM", "<b>", "x" * 40])
def test_veredito_invalido_e_recusado(port: int, folder: Path, verdict: str):
    first = json.loads(call(port, "GET", "/api/items")[2])["items"][0]["id"]

    assert vote(port, first, verdict)[0] == 400
    assert saved(folder)[0]["veredito"] == ""


def test_id_desconhecido_da_404(port: int):
    assert vote(port, "L9999", "sim")[0] == 404


@pytest.mark.parametrize(
    "payload", [b"nao-e-json", b"[]", b'{"id": 1, "verdict": "sim"}', b'{"id": "L0001"}']
)
def test_corpo_malformado_da_400(port: int, payload: bytes):
    status = call(port, "POST", "/api/verdict", payload, {"Content-Type": "application/json"})[0]

    assert status == 400


def test_exige_content_type_json_para_impedir_post_de_outra_pagina(port: int, folder: Path):
    first = json.loads(call(port, "GET", "/api/items")[2])["items"][0]["id"]
    payload = json.dumps({"id": first, "verdict": "sim"}).encode()

    for content_type in ("text/plain", "application/x-www-form-urlencoded"):
        status = call(port, "POST", "/api/verdict", payload, {"Content-Type": content_type})[0]
        assert status == 415

    assert saved(folder)[0]["veredito"] == ""


def test_origem_de_outro_site_e_recusada(port: int, folder: Path):
    first = json.loads(call(port, "GET", "/api/items")[2])["items"][0]["id"]

    assert vote(port, first, "sim", Origin="https://evil.example")[0] == 403
    assert vote(port, first, "sim", Origin=f"http://127.0.0.1:{port}")[0] == 200


def test_host_diferente_de_localhost_e_recusado_contra_dns_rebinding(port: int):
    status = call(port, "GET", "/api/items", headers={"Host": "evil.example"})[0]

    assert status == 403


def test_corpo_gigante_e_recusado(port: int):
    big = b'{"id": "' + b"A" * 5000 + b'", "verdict": "sim"}'

    status = call(port, "POST", "/api/verdict", big, {"Content-Type": "application/json"})[0]

    assert status == 413


def test_rotas_e_metodos_desconhecidos(port: int):
    assert call(port, "GET", "/nada")[0] == 404
    assert call(port, "PUT", "/api/verdict")[0] == 405
    assert call(port, "DELETE", "/api/items")[0] == 405
    assert call(port, "GET", "/api/verdict")[0] == 404
