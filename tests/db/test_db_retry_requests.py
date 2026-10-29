"""Pedidos de reprocessamento: cada clique ganha um id e um resumo do que aconteceu depois."""

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


@pytest.fixture
def world(schema_name: str) -> dict[str, Any]:
    data = seed(schema_name)
    return dict(reports=ReportRepository(schema_name, corpus_schema=schema_name), **data)


def settle(reports: ReportRepository, name: str, state: str, **columns: Any) -> None:
    """Simula o que os workers fazem com uma notícia reaberta."""
    sets = ",".join(f"{k}=%s" for k in ("state", *columns))
    with reports.connection() as db:
        db.execute(
            f"UPDATE bm_articles SET {sets} WHERE hash=%s", (state, *columns.values(), key(name))
        )  # noqa: S608


def only(reports: ReportRepository) -> dict[str, Any]:
    [request] = reports.retry_requests()
    return request


def test_reopening_returns_an_id_and_records_what_was_asked(world):
    reports = world["reports"]

    done = reports.reopen(dict(reason_class="transient"))

    assert done["requeued"] == 1 and len(done["retry_id"]) == 32
    request = only(reports)
    assert request["id"] == done["retry_id"]
    assert request["requested"] == 1
    assert request["filters"] == dict(reason_class="transient")
    assert request["created_at"]


def test_the_summary_follows_the_news_until_they_are_resolved(world):
    reports = world["reports"]
    reports.reopen(dict(source_id=world["sites"][1]["id"]))  # blocked + flaky

    start = only(reports)
    assert (start["requested"], start["in_progress"], start["finished"]) == (2, 2, False)

    settle(reports, "flaky", "done", outcome="persisted")
    settle(reports, "blocked", "failed", reason="http_403", reason_class="blocked")

    end = only(reports)
    assert end["in_progress"] == 0 and end["finished"] is True
    assert (end["persisted"], end["done_other"], end["failed"]) == (1, 0, 1)
    assert end["reasons"] == [dict(reason="http_403", n=1)]


def test_known_and_discarded_outcomes_count_as_other_completions(world):
    reports = world["reports"]
    reports.reopen(dict(reason_class="transient"))

    settle(reports, "flaky", "done", outcome="no_entities")

    request = only(reports)
    assert (request["persisted"], request["done_other"]) == (0, 1)


def test_nothing_to_reopen_leaves_no_request_behind(world):
    reports = world["reports"]

    done = reports.reopen(dict(reason="motivo_que_nao_existe"))

    assert done == dict(requeued=0, retry_id=None)
    assert reports.retry_requests() == []


def test_a_newer_request_takes_the_news_it_reopens_again(world):
    reports = world["reports"]
    first = reports.reopen(dict(reason_class="transient"))["retry_id"]
    settle(reports, "flaky", "failed", reason="http_503", reason_class="transient")

    second = reports.reopen(dict(hashes=[key("flaky")]))["retry_id"]

    newest, oldest = reports.retry_requests()
    assert (newest["id"], oldest["id"]) == (second, first)
    assert newest["in_progress"] == 1 and newest["superseded"] == 0
    assert oldest["in_progress"] == 0 and oldest["superseded"] == 1


def test_the_filter_names_the_site_and_never_stores_every_hash(world):
    reports = world["reports"]
    site = world["sites"][1]

    reports.reopen(dict(source_id=site["id"]))
    reports.reopen(dict(hashes=[key("broken")]))

    hashes, by_site = reports.retry_requests()
    assert hashes["filters"] == dict(hashes=1)
    assert by_site["source_name"] == site["name"]


def test_the_pending_screen_carries_the_recent_requests(world):
    reports = world["reports"]
    done = reports.reopen(dict(reason_class="transient"))

    assert [r["id"] for r in reports.pending()["retries"]] == [done["retry_id"]]


def test_only_the_most_recent_requests_are_listed(world):
    reports = world["reports"]
    for _ in range(12):
        settle(reports, "flaky", "failed", reason="http_503", reason_class="transient")
        reports.reopen(dict(hashes=[key("flaky")]))

    assert len(reports.retry_requests()) == 10


def test_retry_keeps_returning_the_plain_count(world):
    reports = world["reports"]

    assert reports.retry(dict(reason_class="transient")) == 1
    with pytest.raises(CatalogError):
        reports.reopen({})
