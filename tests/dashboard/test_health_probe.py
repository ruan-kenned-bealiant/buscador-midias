# ruff: noqa: F811 -- fixtures importadas de test_catalog_health_postgres são reutilizadas por nome.
"""Rotina de saúde com as regras novas: sonda leve nos ativos e portão de frescor nos inativos."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from test_catalog_health_postgres import health, old_news  # noqa: F401
from test_catalog_postgres import local, schema  # noqa: F401

from buscador_midias.catalog.health_worker import HealthWorker
from buscador_midias.catalog.store import CatalogError
from buscador_midias.net.fetcher import FetchResult

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def rss(*days_ago: int) -> bytes:
    now = datetime.now(UTC)
    items = "".join(
        f"<item><link>https://jose.example/n{n}</link><pubDate>"
        f"{(now - timedelta(days=d)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for n, d in enumerate(days_ago)
    )
    return f"<rss><channel>{items}</channel></rss>".encode()


class Probe:
    """Leitor falso da sonda: devolve sempre a resposta combinada e conta as leituras."""

    def __init__(self, body: bytes | None = None, status: int = 200, error: str | None = None):
        self.body, self.status, self.error, self.asked = body, status, error, []

    def __call__(self, link):
        return self

    def get(self, url):
        self.asked.append(url)
        return FetchResult(url, self.status, self.body or b"", self.error, url)


def settings(health, kind, **changes):
    current = next(s for s in health.settings() if s["kind"] == kind)
    data = dict(
        enabled=True, interval_minutes=1440, workers=2, max_attempts=2, version=current["version"]
    )
    return health.configure(kind, {**data, **changes})


def run_probe(health, probe, *, kind="active"):
    if kind == "active":
        settings(health, "active", mode="probe")
    worker = HealthWorker(health, old_news, probe_reader=probe)
    run = health.create(kind)
    worker.recover()
    worker.execute_run(worker.next_run())
    return run


def state(health):
    link = health.store.endpoint(1)
    with health.connection() as db:
        row = db.execute("SELECT category,failures FROM link_health WHERE link_id=1").fetchone()
        events = [
            r["action"]
            for r in db.execute("SELECT action FROM events WHERE entity_id=1 ORDER BY id")
        ]
    return link, row, events


def test_a_fresh_link_stays_active_and_is_recorded_healthy(health):
    probe = Probe(rss(1, 2))
    run = run_probe(health, probe)
    link, row, events = state(health)
    assert link["active"] and (row["category"], row["failures"]) == ("healthy", 0)
    assert not any(e.startswith("health_") for e in events)
    assert probe.asked == ["https://jose.example/feed/"]
    assert health.details(run["id"])["items"][0]["state"] == "completed"


def test_a_probe_run_never_runs_the_slow_full_test(health):
    probe = Probe(rss(1))
    run_probe(health, probe)
    with health.connection() as db:
        # O teste completo amostra artigos; a sonda lê só o documento, uma vez.
        assert len(probe.asked) == 1
        assert db.execute("SELECT count(*) n FROM link_test_results").fetchone()["n"] == 0


def test_a_link_stopped_for_over_a_month_is_inactivated_with_an_audit_event(health):
    run_probe(health, Probe(rss(60, 90)))
    link, row, events = state(health)
    assert not link["active"] and row["category"] == "stale"
    assert "health_inactivate" in events


def test_a_link_stopped_for_over_a_year_is_deleted(health):
    run_probe(health, Probe(rss(400, 800)))
    with pytest.raises(CatalogError, match="excluído"):
        health.store.endpoint(1)
    assert "health_delete" in state_events(health)


def state_events(health):
    with health.connection() as db:
        return [r["action"] for r in db.execute("SELECT action FROM events WHERE entity_id=1")]


@pytest.mark.parametrize("path", ["receitas/feed.xml", "sitemap/2026/09/01.xml"])
def test_a_useless_theme_or_closed_period_link_is_deleted_without_being_read(health, path):
    link = health.store.endpoint(1)
    health.store.save(
        "endpoint", dict(version=link["version"], url=f"https://jose.example/{path}"), 1
    )
    probe = Probe(rss(1))
    run_probe(health, probe)
    with pytest.raises(CatalogError):
        health.store.endpoint(1)
    assert probe.asked == []


def test_a_sports_link_with_fresh_content_waits_for_human_review(health):
    link = health.store.endpoint(1)
    health.store.save(
        "endpoint", dict(version=link["version"], url="https://jose.example/esportes/feed"), 1
    )
    run_probe(health, Probe(rss(1)))
    link, row, _ = state(health)
    assert link["active"] and row["category"] == "review"


def test_a_missing_page_is_inactivated_at_once(health):
    run_probe(health, Probe(status=404))
    link, row, _ = state(health)
    assert not link["active"] and (row["category"], row["failures"]) == ("access_error", 1)


def test_a_passing_failure_inactivates_only_on_the_second_check_in_a_row(health):
    run_probe(health, Probe(status=503))
    link, row, _ = state(health)
    assert link["active"] and row["failures"] == 1
    run_probe(health, Probe(status=503))
    link, row, _ = state(health)
    assert not link["active"] and row["failures"] == 2


def test_a_success_in_between_clears_the_failure_count(health):
    run_probe(health, Probe(status=503))
    run_probe(health, Probe(rss(1)))
    assert state(health)[1]["failures"] == 0
    run_probe(health, Probe(status=503))
    link, row, _ = state(health)
    assert link["active"] and row["failures"] == 1


def test_an_edit_during_the_check_wins_and_the_link_is_not_touched(health):
    settings(health, "active", mode="probe")
    run = health.create("active")
    link = health.store.endpoint(1)
    health.store.save("endpoint", dict(version=link["version"], label="Editado"), 1)
    worker = HealthWorker(health, old_news, probe_reader=Probe(rss(60)))
    worker.recover()
    worker.execute_run(worker.next_run())
    assert health.store.endpoint(1)["active"]
    assert health.details(run["id"])["items"][0]["state"] == "skipped"


def inactive_run(health, probe, **options):
    link = health.store.endpoint(1)
    health.store.save("endpoint", dict(version=link["version"], active=False), 1)
    worker = HealthWorker(health, old_news, probe_reader=probe)
    run = health.create(
        "recovery", options=dict(scope="inactive", activate_recovered=True, **options)
    )
    worker.recover()
    worker.execute_run(worker.next_run())
    return run


def test_an_inactive_link_comes_back_only_with_text_proof_and_news_this_month(health):
    run = inactive_run(health, Probe(rss(2, 3)))
    assert health.store.endpoint(1)["active"]
    assert health.details(run["id"])["items"][0]["activated"]


def test_a_healthy_but_stale_inactive_link_is_not_reactivated(health):
    run = inactive_run(health, Probe(rss(60, 90)))
    assert not health.store.endpoint(1)["active"]
    item = health.details(run["id"])["items"][0]
    assert item["category"] == "healthy" and not item["activated"]
    assert "parado" in item["detail"] or "frescor" in item["detail"].lower()


def test_an_inactive_theme_link_is_never_reactivated(health):
    link = health.store.endpoint(1)
    health.store.save(
        "endpoint", dict(version=link["version"], url="https://jose.example/horoscopo/feed"), 1
    )
    run = inactive_run(health, Probe(rss(1)))
    assert not health.store.endpoint(1)["active"]
    assert not health.details(run["id"])["items"][0]["activated"]


def test_a_link_inactivated_by_this_check_waits_for_the_next_one(health):
    settings(health, "active", mode="probe")
    settings(health, "recovery", scope="inactive")
    active = health.create("active")
    recovery = health.create("recovery", options=dict(activate_recovered=True))
    assert active["total"] == 1 and recovery["total"] == 0
    worker = HealthWorker(health, old_news, probe_reader=Probe(rss(60)))
    worker.recover()
    worker.execute_run(worker.next_run())
    worker.execute_run(worker.next_run())
    assert not health.store.endpoint(1)["active"]
