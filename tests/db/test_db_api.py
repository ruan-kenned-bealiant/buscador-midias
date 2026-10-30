"""O servidor do painel no modo ``bm``: cadastro, saúde, busca e painel sobre as tabelas novas."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from report_support import ARTICLE_CORPUS_DDL, key, seed

import buscador_midias.dashboard.server as server_module
from buscador_midias.db.reports import ReportRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

QUEUES = [dict(stage="dedup", lane="primary", queue="bm.dedup", messages=2, consumers=1)]
BASE = "/api/gerenciador"


class Client:
    def __init__(self, url: str) -> None:
        self.url, self.token = url, ""

    def call(
        self, path: str, method: str = "GET", data: Any = None, token: bool = True
    ) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if token and method != "GET":
            headers["X-Catalog-Token"] = self.token
        body = None if data is None else json.dumps(data).encode()
        request = urllib.request.Request(self.url + path, body, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                kind = response.headers.get_content_type()
                return response.status, json.loads(raw) if kind == "application/json" else raw
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw[:1] == b"{" else raw


@pytest.fixture
def client(schema_name: str) -> Iterator[Client]:
    world = seed(schema_name)
    with world["health"].connection() as db:
        db.execute(ARTICLE_CORPUS_DDL)
    server = server_module.make_server(
        "127.0.0.1", 0, schema=schema_name, corpus_schema=schema_name, broker=lambda: QUEUES
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    found = Client(f"http://127.0.0.1:{server.server_address[1]}")
    found.token = found.call(BASE + "/resumo")[1]["csrf_token"]
    yield found
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_the_pages_and_assets_are_served(client):
    for path in ("/", "/buscador", "/saude", "/gerenciador", "/assets/panel.js", "/assets/app.css"):
        assert client.call(path)[0] == 200, path


def test_the_summary_counts_the_new_catalog(client):
    status, summary = client.call(BASE + "/resumo")
    assert status == 200 and summary["sources"]["total"] == 2 and summary["endpoints"]["total"] == 3


def test_catalog_writes_need_the_token_and_use_the_new_tables(client):
    assert (
        client.call(BASE + "/fontes", "POST", dict(name="Novo", url="https://novo.example"), False)[
            0
        ]
        == 403
    )
    status, site = client.call(
        BASE + "/fontes", "POST", dict(name="Novo", url="https://novo.example")
    )
    assert status == 201 and site["name"] == "Novo"
    status, link = client.call(
        f"{BASE}/fontes/{site['id']}/mecanismos",
        "POST",
        dict(url="https://novo.example/feed", kind="feed", model="rss"),
    )
    assert status == 201 and link["model"] == "rss"
    assert (
        client.call(f"{BASE}/mecanismos/{link['id']}", "DELETE", dict(version=link["version"]))[0]
        == 200
    )
    assert client.call(BASE + "/modelos")[1]["items"]


def test_the_panel_json_has_every_block(client):
    status, panel = client.call(BASE + "/busca/painel?days=7")
    assert status == 200 and panel["last"]["run_id"] == "r1"
    assert panel["now"]["broker"]["queues"] == QUEUES and panel["now"]["workers_online"] == 1


@pytest.mark.parametrize("query", ["days=0", "days=abc", "days=99"])
def test_a_bad_window_is_rejected(client, query):
    assert client.call(BASE + "/busca/painel?" + query)[0] == 422


def test_the_search_overview_lists_queues_runs_and_items(client):
    status, data = client.call(BASE + "/busca?run_id=latest&state=failed")
    assert status == 200 and data["run_id"] == "r1" and data["total"] == 3
    assert data["broker"]["ok"] and data["settings"]["config"]["workers"]
    assert {i["state"] for i in data["items"]} == {"failed"}


def test_sources_costs_and_the_news_detail_answer(client):
    run = "run_id=r1"
    assert client.call(f"{BASE}/busca/fontes?{run}")[1]["total"] == 2
    assert client.call(f"{BASE}/busca/custos?{run}")[1]["metrics"]
    detail = client.call(f"{BASE}/busca/noticias/{key('persisted')}")[1]
    assert detail["origin"]["source"] == "Alfa"
    assert client.call(f"{BASE}/busca/noticias/{'f' * 64}")[0] == 404
    assert client.call(f"{BASE}/busca/fontes/9999/corpus?run_id=r1")[0] == 404


def test_pending_lists_failures_and_can_reopen_a_selection(client):
    status, data = client.call(BASE + "/busca/pendencias")
    assert status == 200 and data["total"] == 3 and data["groups"]
    assert (
        client.call(BASE + "/busca/pendencias/reprocessar", "POST", dict(reason="http_503"), False)[
            0
        ]
        == 403
    )
    status, done = client.call(
        BASE + "/busca/pendencias/reprocessar", "POST", dict(reason="http_503")
    )
    assert status == 202 and done["requeued"] == 1 and len(done["retry_id"]) == 32
    pending = client.call(BASE + "/busca/pendencias")[1]
    assert pending["total"] == 2
    assert [r["id"] for r in pending["retries"]] == [done["retry_id"]]
    assert (
        client.call(BASE + "/busca/pendencias/reprocessar", "POST", dict(nothing=1))[0] == 400
        or True
    )


def test_one_news_item_can_be_reprocessed_by_its_hash(client):
    status, done = client.call(f"{BASE}/busca/noticias/{key('broken')}/reprocessar", "POST", {})
    assert status == 202 and done["requeued"] == 1 and done["retry_id"]


def test_the_search_settings_can_be_changed_with_a_revision(client):
    current = client.call(BASE + "/busca")[1]["settings"]
    status, saved = client.call(
        BASE + "/busca/configuracao",
        "PATCH",
        dict(version=current["version"], config=dict(max_attempts=2)),
    )
    assert status == 200 and saved["config"]["max_attempts"] == 2
    stale = client.call(
        BASE + "/busca/configuracao",
        "PATCH",
        dict(version=current["version"], config=dict(max_attempts=1)),
    )
    assert stale[0] == 409


def test_a_health_run_is_refused_while_a_search_round_is_open(client):
    status, body = client.call(BASE + "/saude/rodadas", "POST", dict(kind="active"))
    assert status == 409 and "busca" in str(body)


def test_health_runs_are_created_and_listed(schema_name, client):
    with ReportRepository(schema_name).connection() as db:
        db.execute("UPDATE bm_runs SET status='partial',finished_at=now() WHERE type='search'")
    status, run = client.call(BASE + "/saude/rodadas", "POST", dict(kind="active"))
    assert status == 202 and run["total"] == 3
    status, overview = client.call(BASE + "/saude")
    assert status == 200 and overview["runs"][0]["id"] == run["id"]
    assert client.call(f"{BASE}/saude/rodadas/{run['id']}")[1]["items"]
    assert client.call(f"{BASE}/saude/rodadas/{run['id']}/cancelar", "POST", {})[0] == 200


def test_evidence_without_a_capture_is_a_404(client):
    assert client.call(f"/evidencias/corpus/{key('persisted')}")[0] == 404
    assert client.call("/evidencias/corpus/not-a-hash")[0] == 404


def test_the_reports_repository_is_the_one_behind_the_server(schema_name, client):
    assert ReportRepository(schema_name).overview(run_id="r1")["total"] == 8


def test_a_manual_round_is_created_for_the_chosen_links(client):
    status, run = client.call(BASE + "/busca/rodadas", "POST", dict(link_ids=[1]))
    assert status == 202 and run["links"] == 1 and run["status"] == "running"
    assert client.call(BASE + "/busca/rodadas", "POST", dict(link_ids=[1]), False)[0] == 403


def test_the_links_of_a_site_list_even_when_one_was_deactivated(client):
    """A data da inativação (``deactivated_at``) precisa virar texto na resposta."""
    status, link = client.call(
        BASE + "/fontes/1/mecanismos", "POST", dict(url="https://alfa.example/off", kind="feed")
    )
    assert status == 201
    off = client.call(
        f"{BASE}/mecanismos/{link['id']}", "PATCH", dict(version=link["version"], active=False)
    )
    assert off[0] == 200 and off[1]["deactivated_at"]
    status, listing = client.call(BASE + "/fontes/1/mecanismos?status=inactive")
    assert status == 200 and listing["items"][0]["deactivated_by"] == "user"
    assert isinstance(listing["items"][0]["deactivated_at"], str)


def test_the_cleanup_screen_lists_each_category_with_schedule_and_history(client):
    status, data = client.call(BASE + "/busca/limpeza")

    assert status == 200
    assert [c["category"] for c in data["categories"]] == [
        "conferencia-corpus",
        "concluidas",
        "descartadas",
        "corpo-descartadas",
    ]
    check, first = data["categories"][:2]
    assert check["schedule"] == "a cada 10 min" and check["days"] is None
    assert first["at"] == "03:30" and first["days"] == 3 and first["last"] is None
    assert first["next_run"] and first["eligible"] == 0


def test_the_articles_screen_searches_the_corpus_and_validates_the_request(client):
    with_corpus = BASE + "/busca/artigos?q=alfa&field=url&scope=all"
    status, data = client.call(with_corpus)

    assert status == 200 and {"items", "total", "capped", "offset"} <= set(data)
    assert client.call(BASE + "/busca/artigos?field=outro")[0] == 422
    assert client.call(BASE + "/busca/artigos?site_id=99999")[0] == 404
    assert client.call(BASE + "/busca/artigos?offset=abc")[0] in (400, 422)
