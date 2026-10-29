"""Consultas das telas de busca: filas, rodadas, notícias, sites, custos e pendências."""

from __future__ import annotations

import os
from typing import Any

import pytest
from report_support import key, seed

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.reports import ReportRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

CORPUS = (
    "CREATE TABLE midias_ia_articles (id BIGINT PRIMARY KEY, titulo TEXT, url TEXT, "
    "published_at TIMESTAMPTZ, evidencia_status TEXT, evidencia_path TEXT, body TEXT, "
    "origin TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
)


@pytest.fixture
def world(schema_name: str) -> dict[str, Any]:
    data = seed(schema_name)
    reports = ReportRepository(schema_name, corpus_schema=schema_name)
    with reports.connection() as db:
        db.execute(CORPUS)
        db.execute(
            "INSERT INTO midias_ia_articles(id,titulo,url,published_at,evidencia_status,"
            "evidencia_path,body) VALUES(7,'Gravada','https://alfa.example/persisted',now(),"
            "'captured','a/b.html','corpo da notícia')"
        )
        db.execute("UPDATE bm_articles SET corpus_article_id=7 WHERE hash=%s", (key("persisted"),))
    return dict(reports=reports, **data)


def states(rows: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    return {(r["stage"], r["state"]): int(r["n"]) for r in rows}


def test_overview_counts_the_queues_by_stage_and_state(world):
    result = world["reports"].overview(run_id="r1")
    queues = states(result["queues"])
    assert queues[("fetch", "failed")] == 2 and queues[("fetch", "running")] == 1
    assert queues[("persist", "succeeded")] == 1 and queues[("context", "pending")] == 1
    assert queues[("discovery", "failed")] == 1 and queues[("discovery", "succeeded")] == 1
    assert result["run_id"] == "r1"


def test_latest_resolves_to_the_newest_run(world):
    assert world["reports"].overview(run_id="latest")["run_id"] == "r1"


def test_overview_without_a_run_covers_every_run(world):
    assert world["reports"].overview()["total"] == 8


def test_the_item_list_is_filtered_by_stage_and_state(world):
    reports = world["reports"]
    failed = reports.overview(run_id="r1", state="failed")
    assert failed["total"] == 3
    assert {i["state"] for i in failed["items"]} == {"failed"}
    assert reports.overview(run_id="r1", stage="context", state="pending")["total"] == 1
    done = reports.overview(run_id="r1", state="succeeded")
    assert {i["outcome"] for i in done["items"]} == {"persisted", "known", "date_unknown"}


def test_the_discovery_stage_lists_the_read_tasks(world):
    result = world["reports"].overview(run_id="r1", stage="discovery")
    assert result["total"] == 3 and {i["kind"] for i in result["items"]} == {"discovery"}
    assert {i["state"] for i in result["items"]} == {"succeeded", "running", "failed"}


def test_an_item_shows_what_a_person_needs_to_decide(world):
    [item] = world["reports"].overview(run_id="r1", state="failed", stage="context")["items"]
    assert item["id"] == key("broken") and item["kind"] == "article"
    assert item["source"] == "Alfa" and item["url"].endswith("/broken")
    assert (item["reason"], item["reason_class"]) == ("Falha interna", "bug")
    assert item["title"] == "Título broken" and item["attempts"] == 1


def test_the_source_filter_restricts_items_and_queues(world):
    beta = world["sites"][1]["id"]
    result = world["reports"].overview(run_id="r1", source_id=beta)
    assert result["total"] == 3 and {i["source"] for i in result["items"]} == {"Beta"}


def test_the_funnel_names_each_result_and_where_the_open_ones_wait(world):
    funnel = {r["status"]: int(r["n"]) for r in world["reports"].overview(run_id="r1")["funnel"]}
    assert funnel["persisted"] == 1 and funnel["known"] == 1 and funnel["failed"] == 3
    assert funnel["fetch"] == 1 and funnel["context"] == 1 and funnel["date_unknown"] == 1


def test_the_run_table_has_the_numbers_the_screen_shows(world):
    runs = {r["id"]: r for r in world["reports"].overview(run_id="r1")["runs"]}
    assert [*runs] == ["r1", "r0"]
    assert (runs["r1"]["links"], runs["r1"]["links_done"]) == (3, 2)
    assert (runs["r1"]["occurrences"], runs["r1"]["unique_articles"]) == (30, 8)
    assert runs["r1"]["state"] == "running" and runs["r0"]["state"] == "completed"
    assert runs["r1"]["primary_pending"] == 3 and runs["r1"]["recovery_pending"] == 0


@pytest.mark.parametrize(
    "kwargs", [dict(stage="nope"), dict(state="nope"), dict(offset=-1), dict(source_id=-1)]
)
def test_invalid_filters_are_rejected(world, kwargs):
    with pytest.raises(CatalogError):
        world["reports"].overview(**kwargs)


def test_metrics_sum_cost_and_calls_by_stage(world):
    rows = {r["stage"]: r for r in world["reports"].metrics(run_id="r1")}
    assert float(rows["context"]["cost_usd"]) == pytest.approx(0.03)
    assert int(rows["context"]["prompt_tokens"]) == 150
    assert int(rows["fetch"]["bright_data_requests"]) == 2


def test_source_progress_summarises_each_site_of_the_run(world):
    result = world["reports"].source_progress("r1")
    assert result["run"]["id"] == "r1" and result["total"] == 2
    sites = {s["name"]: s for s in result["sources"]}
    alfa, beta = sites["Alfa"], sites["Beta"]
    assert (alfa["links"], alfa["links_done"], alfa["articles"]) == (2, 1, 5)
    assert alfa["statuses"]["persisted"] == 1 and alfa["statuses"]["known"] == 1
    assert beta["statuses"] == {"context": 1, "failed": 2}
    assert beta["errors"] == 2 and beta["state"] == "running"
    assert alfa["discovery_complete"] is False and alfa["progress"] is None
    assert result["summary"] == {"running": 2}


def test_source_progress_searches_by_name_and_paginates(world):
    result = world["reports"].source_progress("r1", 0, "bet")
    assert [s["name"] for s in result["sources"]] == ["Beta"] and result["total"] == 1


def test_source_progress_of_an_unknown_run_is_a_404(world):
    with pytest.raises(CatalogError) as missing:
        world["reports"].source_progress("c" * 32)
    assert missing.value.status == 404


def test_a_closed_site_shows_completion_with_pending_errors_as_partial(world):
    with world["reports"].connection() as db:
        db.execute("UPDATE bm_run_links SET discovery_done=true,state='completed'")
        db.execute("UPDATE bm_discovery_tasks SET state='done'")
        db.execute(
            "UPDATE bm_articles SET state='failed',failed_stage='fetch' WHERE state='running'"
        )
        db.execute("UPDATE bm_articles SET state='done',outcome='known' WHERE state='pending'")
    beta = next(s for s in world["reports"].source_progress("r1")["sources"] if s["name"] == "Beta")
    assert beta["state"] == "partial" and beta["progress"] == 100


def test_corpus_articles_lists_only_what_reached_the_corpus(world):
    result = world["reports"].corpus_articles(world["sites"][0]["id"], "r1")
    assert result["total"] == 1 and result["source"] == "Alfa"
    [article] = result["articles"]
    assert article["title"] == "Gravada" and article["evidence_url"] == (
        "/evidencias/corpus/" + key("persisted")
    )


def test_corpus_articles_of_a_site_outside_the_run_is_a_404(world):
    with pytest.raises(CatalogError) as missing:
        world["reports"].corpus_articles(999, "r1")
    assert missing.value.status == 404


def test_the_article_detail_has_the_origin_and_the_corpus_body(world):
    detail = world["reports"].article(key("persisted"))
    assert detail["article"]["payload"]["title"] == "Título persisted"
    assert detail["article"]["payload"]["body"] == "corpo da notícia"
    assert detail["origin"]["source"] == "Alfa" and detail["origin"]["document_url"].endswith(
        "/feed"
    )
    with pytest.raises(CatalogError) as missing:
        world["reports"].article("f" * 64)
    assert missing.value.status == 404


# --- pendências ---------------------------------------------------------------------------------


def test_pending_groups_failures_by_stage_reason_and_class(world):
    result = world["reports"].pending()
    groups = {(g["failed_stage"], g["reason"]): g for g in result["groups"]}
    assert groups[("fetch", "http_403")]["reason_class"] == "blocked"
    assert groups[("fetch", "http_503")]["retryable"] is True
    assert result["total"] == 3 and {i["state"] for i in result["items"]} == {"failed"}


def test_pending_can_be_filtered(world):
    reports = world["reports"]
    assert reports.pending(reason_class="transient")["total"] == 1
    assert reports.pending(stage="context")["total"] == 1
    assert reports.pending(reason="http_403")["total"] == 1
    assert reports.pending(source_id=world["sites"][1]["id"])["total"] == 2
    assert reports.pending(run_id="r0")["total"] == 0


def test_retry_reopens_exactly_what_the_filter_selects(world):
    reports = world["reports"]
    assert reports.retry(dict(reason_class="transient")) == 1
    [item] = reports.overview(run_id="r1", state="retry")["items"]
    assert item["id"] == key("flaky") and item["stage"] == "fetch"
    assert reports.pending()["total"] == 2


def test_retry_by_site_and_by_a_single_news_item(world):
    reports = world["reports"]
    assert reports.retry(dict(source_id=world["sites"][1]["id"])) == 2
    assert reports.retry(dict(hashes=[key("broken")])) == 1
    assert reports.pending()["total"] == 0


def test_retry_without_any_filter_needs_explicit_confirmation(world):
    reports = world["reports"]
    with pytest.raises(CatalogError):
        reports.retry({})
    assert reports.retry(dict(everything=True)) == 3


def test_retry_rejects_unknown_filters(world):
    with pytest.raises(CatalogError):
        world["reports"].retry(dict(reason_class="weird"))
    with pytest.raises(CatalogError):
        world["reports"].retry(dict(nope=1))


def test_the_evidence_of_a_news_item_is_the_one_registered_in_the_corpus(world):
    assert world["reports"].evidence(key("persisted")) == dict(path="a/b.html", status="captured")
    assert world["reports"].evidence(key("known")) is None
    assert world["reports"].evidence("not-a-hash") is None
