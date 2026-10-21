import csv
import json
import threading
import urllib.error
import urllib.request

import pytest

from buscador_midias.dashboard.server import make_server


@pytest.fixture
def selection_server(tmp_path):
    source = dict(
        fonte="example.com",
        nome="<img src=x onerror=alert(1)>",
        pais="Brasil",
        url="https://example.com",
        frequentes="1",
        periodicos="1",
        excluidos="0",
        inconclusivos="1",
        lacuna_proveniencia="False",
        fonte_parcial="False",
        possui_texto_confirmado="True",
    )
    (tmp_path / "fontes.json").write_text(json.dumps([source]))
    (tmp_path / "manifesto.json").write_text(json.dumps({"fontes_frequentes": 1}))
    (tmp_path / "plano-frequente.json").write_text('[{"chave":"example.com"}]')
    records = []
    for category, url in [
        ("frequente", "https://example.com/rss.xml"),
        ("periodico", "https://example.com/sitemap.xml"),
        ("inconclusivo", "feed:example.com"),
    ]:
        records.append(
            dict(
                fonte="example.com",
                mecanismo=url,
                classificacao=category,
                explicacao="<script>não executar</script>",
                tipo_reutilizavel="feed",
                exemplo_textual="https://example.com/noticia/exemplo",
                exemplo_audio="",
                documentos_processados="" if category == "inconclusivo" else "1",
                textos_confirmados="1",
                documentos_pendentes="0",
                endpoint_reutilizavel=str(category != "inconclusivo"),
            )
        )
    with (tmp_path / "mecanismos.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)

    def forbidden_database():
        raise AssertionError("A seleção não deve abrir o banco")

    server = make_server(forbidden_database, "127.0.0.1", 0, selection_directory=tmp_path)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", tmp_path
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as response:
        return response.code, response.read()


def test_selecao_funciona_sem_banco_e_preserva_custo_ausente(selection_server):
    base, _ = selection_server
    assert get(base + "/selecao")[0] == 200
    status, body = get(base + "/api/selecao")
    data = json.loads(body)
    assert status == 200 and len(data["sources"]) == 1
    assert data["sources"][0]["name"].startswith("<img")  # A tela usa textContent.
    _, body = get(base + "/api/selecao/mecanismos?site=example.com&categoria=inconclusivo")
    data = json.loads(body)
    assert data["total"] == 1 and data["items"][0]["documents"] is None
    assert not data["items"][0]["usable"]


def test_filtros_paginacao_e_downloads_permitidos(selection_server):
    base, _ = selection_server
    _, first = get(base + "/api/selecao/mecanismos?site=example.com&limit=1")
    _, second = get(base + "/api/selecao/mecanismos?site=example.com&limit=1&offset=1")
    assert json.loads(first)["items"][0]["url"] != json.loads(second)["items"][0]["url"]
    assert get(base + "/api/selecao/mecanismos?site=unknown")[0] == 404
    assert get(base + "/api/selecao/mecanismos?site=example.com&categoria=invalid")[0] == 400
    assert get(base + "/api/selecao/mecanismos?site=example.com&offset=-1")[0] == 400
    assert get(base + "/api/selecao/mecanismos?site=example.com&limit=1000")[0] == 400
    assert get(base + "/api/selecao/arquivo?nome=plano-frequente.json")[0] == 200
    assert get(base + "/api/selecao/arquivo?nome=../.env")[0] == 400


def test_manifesto_atualiza_retrato_e_pacote_ausente_informa_indisponibilidade(selection_server):
    base, directory = selection_server
    assert get(base + "/api/selecao")[0] == 200
    (directory / "fontes.json").write_text("[]")
    (directory / "manifesto.json").write_text('{"fontes_frequentes": 0, "alterado": true}')
    assert json.loads(get(base + "/api/selecao")[1])["sources"] == []
    (directory / "manifesto.json").unlink()
    assert get(base + "/api/selecao")[0] == 503
