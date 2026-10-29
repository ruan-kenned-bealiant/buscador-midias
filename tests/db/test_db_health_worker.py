"""O worker de saúde conduzindo rodadas reais sobre ``bm_*`` (rede e sonda simuladas)."""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from health_support import FakeReader, FreshProbe, build, old_news, set_state
from psycopg.types.json import Jsonb

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.health import HealthRepository
from buscador_midias.db.health_worker import HealthWorker
from buscador_midias.net.fetcher import FetchResult

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.fixture
def world(schema_name: str) -> dict[str, Any]:
    health, catalog, rows = build(schema_name)
    return dict(health=health, catalog=catalog, **rows)


def execute(health: HealthRepository, factory=old_news, probe=None, **limits: Any) -> HealthWorker:
    worker = HealthWorker(health, factory, probe_reader=probe or FreshProbe())
    for name, value in limits.items():
        setattr(worker, name, value)
    worker.recover()
    run = worker.next_run()
    assert run is not None
    worker.execute_run(run)
    return worker


def sql(health: HealthRepository, query: str, *args: Any) -> list[dict[str, Any]]:
    with health.connection() as db:
        cursor = db.execute(query, args)
        return cursor.fetchall() if cursor.description else []


def link_row(health: HealthRepository, ident: int = 1) -> dict[str, Any]:
    return sql(health, "SELECT * FROM bm_links WHERE id=%s", ident)[0]


def state_row(health: HealthRepository, ident: int = 1) -> dict[str, Any] | None:
    rows = sql(health, "SELECT * FROM bm_link_state WHERE link_id=%s", ident)
    return rows[0] if rows else None


def events(health: HealthRepository, ident: int = 1) -> list[str]:
    return [
        r["action"]
        for r in sql(
            health,
            "SELECT action FROM bm_events WHERE entity='endpoint' AND entity_id=%s ORDER BY id",
            ident,
        )
    ]


def detail(health: HealthRepository, run: dict[str, Any]) -> dict[str, Any]:
    return health.details(run["id"])


def deactivate(world: dict[str, Any]) -> None:
    link = world["catalog"].endpoint(1)
    world["catalog"].save("endpoint", dict(version=link["version"], active=False), 1)


# --- teste completo ---------------------------------------------------------------------------


def test_an_active_run_reuses_the_real_test_ignores_the_date_and_keeps_the_catalog(world):
    health, catalog = world["health"], world["catalog"]
    before = catalog.endpoint(1)
    run = health.create("active")
    execute(health)
    shown = detail(health, run)
    assert shown["run"]["status"] == "completed"
    [row] = shown["items"]
    assert row["category"] == "healthy" and row["attempts"] == 1
    assert row["result"]["text_samples"] == 1
    assert row["result"]["parameters"]["lookback_days"] == 0
    assert catalog.endpoint(1) == before
    assert health.overview()["runs"][0]["counts"] == {"healthy": 1}


def test_the_closed_run_summarises_its_decisions_and_schedules_the_next(world):
    health = world["health"]
    run = health.create("active")
    execute(health)
    [stored] = sql(health, "SELECT status,finished_at,stats,stats_final FROM bm_runs")
    assert stored["status"] == "completed" and stored["finished_at"] is not None
    assert stored["stats"]["counts"] == {"healthy": 1} and stored["stats_final"]
    [setting] = [s for s in health.settings() if s["kind"] == "active"]
    assert setting["next_run_at"] > datetime.now(UTC) and run["id"]


def test_the_last_check_is_recorded_with_the_configuration_it_proved(world):
    health = world["health"]
    run = health.create("active")
    execute(health)
    row = state_row(health)
    assert row and row["health_category"] == "healthy" and row["health_run_id"] == run["id"]
    assert row["health_config"] == dict(
        url="https://jose.example/feed/",
        kind="feed",
        model="rss",
        model_version=1,
        source_url="https://jose.example",
        access_transport="direct",
    )


def test_recovery_reports_a_recovered_link_but_leaves_it_inactive(world):
    health = world["health"]
    deactivate(world)
    set_state(
        health,
        1,
        last_test=dict(
            url="https://jose.example/feed/",
            source_url="https://jose.example",
            model="rss",
            model_version=1,
            endpoint_accessible=True,
            samples=[dict(accessed=False, status=403)],
        ),
    )
    run = health.create("recovery")
    assert run["total"] == 1
    execute(health)
    assert detail(health, run)["items"][0]["category"] == "healthy"
    assert world["catalog"].endpoint(1)["active"] is False
    assert health.create("recovery")["total"] == 0  # recuperado não é retestado de novo


def blocked(config, source, progress, factory):
    return dict(endpoint_accessible=False, endpoint_status=403, outcome="blocked")


def test_retries_are_limited_and_every_attempt_is_kept(world):
    health = world["health"]
    calls: list[str] = []

    def recording(config, source, progress, factory):
        calls.append(config["test_id"])
        return blocked(config, source, progress, factory)

    run = health.create("active")
    with patch("buscador_midias.db.health_worker.run_test", recording):
        execute(health)
    [row] = detail(health, run)["items"]
    assert len(calls) == 2 and row["attempts"] == 2
    assert [a["attempt"] for a in row["history"]] == [1, 2]
    assert row["category"] == "access_error"
    deactivate(world)
    assert health.create("recovery")["total"] == 1  # a falha de acesso da saúde vale como prova


def test_an_interrupted_item_goes_back_to_pending_and_a_cancel_closes_the_run(world):
    health = world["health"]
    run = health.create("active")
    HealthWorker(health, old_news).next_run()
    sql(health, "UPDATE bm_run_links SET state='running'")
    replacement = HealthWorker(health, old_news)
    replacement.recover()
    assert sql(health, "SELECT state,attempts FROM bm_run_links")[0] == dict(
        state="pending", attempts=0
    )
    health.cancel(run["id"])
    replacement.execute_run(replacement.next_run())
    assert detail(health, run)["run"]["status"] == "cancelled"
    assert detail(health, run)["items"][0]["state"] == "cancelled"


def test_an_edit_during_the_run_makes_the_item_skipped(world):
    health, catalog = world["health"], world["catalog"]
    run = health.create("active")
    link = catalog.endpoint(1)
    catalog.save("endpoint", dict(version=link["version"], search_interval_seconds=900), 1)
    execute(health)
    assert detail(health, run)["items"][0]["state"] == "skipped"


def test_a_worker_that_lost_the_run_writes_nothing(world):
    health = world["health"]
    run = health.create("active")
    worker = HealthWorker(health, old_news)
    claimed = worker.next_run()
    assert claimed
    sql(health, "UPDATE bm_runs SET owner='another'")
    worker.execute_run(claimed)
    assert detail(health, run)["items"][0]["state"] == "pending"


def test_one_test_per_host_and_several_hosts_in_parallel(world, monkeypatch):
    health, catalog = world["health"], world["catalog"]
    catalog.save("endpoint", dict(url="https://jose.example/second", kind="feed"), source_id=1)
    other = catalog.save("source", dict(name="Outro", url="https://other.example"))
    catalog.save(
        "endpoint", dict(url="https://other.example/feed/", kind="feed"), source_id=other["id"]
    )
    running: set[str] = set()
    lock = threading.Lock()
    overlaps: list[int] = []
    barrier = threading.Barrier(2, timeout=5)

    def check(config, source, progress, factory):
        host = source["url"]
        with lock:
            assert host not in running
            running.add(host)
            overlaps.append(len(running))
        if config["url"].endswith("feed/"):
            barrier.wait()
        with lock:
            running.remove(host)
        return dict(usable_for_news=True)

    monkeypatch.setattr("buscador_midias.db.health_worker.run_test", check)
    health.create("active")
    execute(health)
    assert max(overlaps) == 2
    assert health.overview()["runs"][0]["counts"] == {"healthy": 3}


def test_a_due_scheduler_runs_each_routine_once_even_with_two_workers(world):
    health = world["health"]
    sql(health, "UPDATE bm_settings SET next_run_at=now()-interval '1 hour'")
    first, second = HealthWorker(health, old_news), HealthWorker(health, old_news)
    first.start()
    second.start()
    try:
        deadline = time.monotonic() + 15
        runs: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            runs = health.overview()["runs"]
            if len(runs) == 2 and all(r["status"] == "completed" for r in runs):
                break
            time.sleep(0.1)
        assert len(runs) == 2 and all(r["status"] == "completed" for r in runs)
        assert {r["kind"] for r in runs} == {"active", "recovery"}
        assert sum(r["total"] for r in runs) == 1
        assert all(s["next_run_at"] > datetime.now(UTC) for s in health.settings())
    finally:
        first.close()
        second.close()


# --- reativação explícita ---------------------------------------------------------------------


def test_an_explicit_scan_activates_textual_proof_and_keeps_the_test(world):
    health = world["health"]
    deactivate(world)
    run = health.create("recovery", options=dict(scope="inactive", activate_recovered=True))
    assert run["total"] == 1
    execute(health)
    [row] = detail(health, run)["items"]
    assert row["activated"] and row["category"] == "healthy"
    updated = world["catalog"].endpoint(1)
    assert updated["active"] and updated["last_test"]["usable_for_news"]
    assert "health_reactivate" in events(health)
    assert link_row(health)["deactivated_by"] is None


def test_empty_results_do_not_reactivate(world, monkeypatch):
    health = world["health"]
    deactivate(world)

    def no_news(config, source, progress, factory):
        return dict(usable_for_news=False, outcome="empty", endpoint_accessible=True)

    monkeypatch.setattr("buscador_midias.db.health_worker.run_test", no_news)
    run = health.create("recovery", options=dict(scope="inactive", activate_recovered=True))
    execute(health)
    assert not world["catalog"].endpoint(1)["active"]
    assert detail(health, run)["items"][0]["category"] == "no_news"


def validation_exclude(world: dict[str, Any], ident: int = 1) -> None:
    set_state(
        world["health"], ident, last_test=dict(endpoint_accessible=False, warnings=["HTTP 403"])
    )
    sql(
        world["health"],
        "UPDATE bm_links SET active=0,deleted_at=now(),deactivated_by='validation',"
        "deactivated_at=now(),version=version+1 WHERE id=%s",
        ident,
    )


RESTORE = dict(scope="auto_excluded_access", activate_recovered=True)


def test_automatic_exclusions_are_restored_with_their_automatic_parent(world):
    health, catalog = world["health"], world["catalog"]
    other = catalog.save(
        "endpoint", dict(url="https://jose.example/other", kind="feed"), source_id=1
    )
    validation_exclude(world)
    validation_exclude(world, other["id"])
    sql(
        health,
        "UPDATE bm_sites SET active=0,deactivated_by='validation',version=version+1 WHERE id=1",
    )
    run = health.create("recovery", options=RESTORE)
    assert run["total"] == 2
    execute(health)
    assert catalog.source(1)["active"]
    assert catalog.endpoint(1)["active"] and catalog.endpoint(other["id"])["active"]
    assert all(i["activated"] for i in detail(health, run)["items"])
    actions = [r["action"] for r in sql(health, "SELECT action FROM bm_events")]
    assert actions.count("health_restore_site") == 1
    assert actions.count("health_reactivate") == 2
    assert sql(health, "SELECT count(*) n FROM bm_sites WHERE deactivated_by IS NOT NULL")[0] == {
        "n": 0
    }


def test_a_restoration_that_fails_keeps_the_link_deleted(world):
    health = world["health"]
    validation_exclude(world)
    run = health.create("recovery", options=RESTORE)
    execute(health, lambda roots, progress: FakeReader({}, progress))
    assert not detail(health, run)["items"][0]["activated"]
    with pytest.raises(CatalogError):
        world["catalog"].endpoint(1)


def test_a_manual_change_during_the_restoration_is_preserved(world):
    health = world["health"]
    validation_exclude(world)
    run = health.create("recovery", options=RESTORE)

    def changed(roots, progress):
        sql(health, "UPDATE bm_links SET version=version+1 WHERE id=1")
        return old_news(roots, progress)

    execute(health, changed)
    assert detail(health, run)["items"][0]["state"] == "skipped"
    with pytest.raises(CatalogError):
        world["catalog"].endpoint(1)


def test_a_restored_site_by_another_run_is_not_accepted(world):
    """Só a reativação do pai feita por esta rodada vale; outra rodada não."""
    health = world["health"]
    validation_exclude(world)
    sql(
        health,
        "UPDATE bm_sites SET active=0,deactivated_by='validation',version=version+1 WHERE id=1",
    )
    run = health.create("recovery", options=RESTORE)
    sql(
        health,
        "UPDATE bm_sites SET active=1,deactivated_by=NULL,version=version+1 WHERE id=1",
    )
    sql(
        health,
        "INSERT INTO bm_events(entity,entity_id,action,changes) VALUES('source',1,"
        "'health_restore_site',%s)",
        Jsonb({"version": [1, 3], "health_run_id": "other-run"}),
    )
    execute(health)
    assert detail(health, run)["items"][0]["state"] == "skipped"


# --- sonda leve (modo probe) ------------------------------------------------------------------


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
        self.body, self.status, self.error = body, status, error
        self.asked: list[str] = []

    def __call__(self, link: dict[str, Any]) -> Probe:
        return self

    def get(self, url: str) -> FetchResult:
        self.asked.append(url)
        return FetchResult(url, self.status, self.body or b"", self.error, url)


def configure(health: HealthRepository, kind: str, **changes: Any) -> None:
    current = next(s for s in health.settings() if s["kind"] == kind)
    health.configure(kind, dict(version=current["version"], **changes))


def run_probe(health: HealthRepository, probe: Any, *, kind: str = "active") -> dict[str, Any]:
    if kind == "active":
        configure(health, "active", mode="probe", workers=2)
    run = health.create(kind)
    assert run
    worker = HealthWorker(health, old_news, probe_reader=probe)
    worker.recover()
    queued = worker.next_run()
    assert queued
    worker.execute_run(queued)
    return run


def test_a_fresh_link_stays_active_and_is_recorded_healthy(world):
    health = world["health"]
    probe = Probe(rss(1, 2))
    run = run_probe(health, probe)
    row = state_row(health)
    assert link_row(health)["active"] and row
    assert (row["health_category"], row["health_failures"]) == ("healthy", 0)
    assert not any(e.startswith("health_") for e in events(health))
    assert probe.asked == ["https://jose.example/feed/"]
    assert detail(health, run)["items"][0]["state"] == "completed"


def test_a_stale_link_is_inactivated_with_the_health_author_and_an_audit_event(world):
    health = world["health"]
    run_probe(health, Probe(rss(60, 90)))
    link, row = link_row(health), state_row(health)
    assert not link["active"] and link["deleted_at"] is None
    assert link["deactivated_by"] == "health" and link["deactivated_at"] is not None
    assert row and row["health_category"] == "stale"
    assert "health_inactivate" in events(health)
    [event] = sql(health, "SELECT changes FROM bm_events WHERE action='health_inactivate'")
    assert event["changes"]["active"] == [1, 0] and event["changes"]["health_run_id"]


def test_a_link_stopped_for_over_a_year_is_deleted(world):
    health = world["health"]
    run_probe(health, Probe(rss(400, 800)))
    link = link_row(health)
    assert link["deleted_at"] is not None and link["deactivated_by"] == "health"
    assert "health_delete" in events(health)


@pytest.mark.parametrize("path", ["receitas/feed.xml", "sitemap/2026/09/01.xml"])
def test_a_useless_theme_or_closed_period_link_is_deleted_without_being_read(world, path):
    health, catalog = world["health"], world["catalog"]
    link = catalog.endpoint(1)
    catalog.save("endpoint", dict(version=link["version"], url=f"https://jose.example/{path}"), 1)
    probe = Probe(rss(1))
    run_probe(health, probe)
    assert link_row(health)["deleted_at"] is not None and probe.asked == []


def test_a_sports_link_with_fresh_content_waits_for_human_review(world):
    health, catalog = world["health"], world["catalog"]
    link = catalog.endpoint(1)
    catalog.save(
        "endpoint", dict(version=link["version"], url="https://jose.example/esportes/feed"), 1
    )
    run_probe(health, Probe(rss(1)))
    row = state_row(health)
    assert link_row(health)["active"] and row and row["health_category"] == "review"


def test_a_missing_page_is_inactivated_at_once(world):
    health = world["health"]
    run_probe(health, Probe(status=404))
    row = state_row(health)
    assert not link_row(health)["active"] and row
    assert (row["health_category"], row["health_failures"]) == ("access_error", 1)


def test_a_passing_failure_inactivates_only_on_the_second_check_in_a_row(world):
    health = world["health"]
    run_probe(health, Probe(status=503))
    row = state_row(health)
    assert link_row(health)["active"] and row and row["health_failures"] == 1
    sql(health, "UPDATE bm_runs SET status='completed'")
    run_probe(health, Probe(status=503))
    row = state_row(health)
    assert not link_row(health)["active"] and row and row["health_failures"] == 2


def test_a_success_in_between_clears_the_failure_count(world):
    health = world["health"]
    run_probe(health, Probe(status=503))
    sql(health, "UPDATE bm_runs SET status='completed'")
    run_probe(health, Probe(rss(1)))
    row = state_row(health)
    assert row and row["health_failures"] == 0
    sql(health, "UPDATE bm_runs SET status='completed'")
    run_probe(health, Probe(status=503))
    row = state_row(health)
    assert link_row(health)["active"] and row and row["health_failures"] == 1


def test_an_edit_during_the_probe_wins_and_the_link_is_not_touched(world):
    health, catalog = world["health"], world["catalog"]
    configure(health, "active", mode="probe")
    run = health.create("active")
    link = catalog.endpoint(1)
    catalog.save("endpoint", dict(version=link["version"], label="Editado"), 1)
    worker = HealthWorker(health, old_news, probe_reader=Probe(rss(60)))
    worker.recover()
    worker.execute_run(worker.next_run())
    assert link_row(health)["active"]
    assert detail(health, run)["items"][0]["state"] == "skipped"


def inactive_run(health: HealthRepository, probe: Any, catalog: Any) -> dict[str, Any]:
    link = catalog.endpoint(1)
    catalog.save("endpoint", dict(version=link["version"], active=False), 1)
    run = health.create("recovery", options=dict(scope="inactive", activate_recovered=True))
    worker = HealthWorker(health, old_news, probe_reader=probe)
    worker.recover()
    worker.execute_run(worker.next_run())
    return run


def test_an_inactive_link_comes_back_only_with_text_proof_and_news_this_month(world):
    run = inactive_run(world["health"], Probe(rss(2, 3)), world["catalog"])
    assert world["catalog"].endpoint(1)["active"]
    assert detail(world["health"], run)["items"][0]["activated"]


def test_a_healthy_but_stale_inactive_link_is_not_reactivated(world):
    run = inactive_run(world["health"], Probe(rss(60, 90)), world["catalog"])
    assert not world["catalog"].endpoint(1)["active"]
    [item] = detail(world["health"], run)["items"]
    assert item["category"] == "healthy" and not item["activated"]
    assert "parado" in item["detail"] or "frescor" in item["detail"].lower()


def test_an_inactive_theme_link_is_never_reactivated(world):
    catalog = world["catalog"]
    link = catalog.endpoint(1)
    catalog.save(
        "endpoint", dict(version=link["version"], url="https://jose.example/horoscopo/feed"), 1
    )
    run = inactive_run(world["health"], Probe(rss(1)), catalog)
    assert not catalog.endpoint(1)["active"]
    assert not detail(world["health"], run)["items"][0]["activated"]


def test_a_link_inactivated_by_this_check_waits_for_the_next_one(world):
    health = world["health"]
    configure(health, "active", mode="probe")
    configure(health, "recovery", scope="inactive")
    active = health.create("active")
    recovery = health.create("recovery", options=dict(activate_recovered=True))
    assert active["total"] == 1 and recovery["total"] == 0
    worker = HealthWorker(health, old_news, probe_reader=Probe(rss(60)))
    worker.recover()
    worker.execute_run(worker.next_run())
    worker.execute_run(worker.next_run())
    assert not world["catalog"].endpoint(1)["active"]


class Concurrent(Probe):
    """Sonda que mede quantas leituras acontecem ao mesmo tempo no mesmo site."""

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.lock, self.now, self.max_seen = threading.Lock(), 0, 0

    def get(self, url: str) -> FetchResult:
        with self.lock:
            self.now += 1
            self.max_seen = max(self.max_seen, self.now)
        time.sleep(0.15)
        with self.lock:
            self.now -= 1
        return super().get(url)


def many_links(world: dict[str, Any], extra: int = 5) -> None:
    for n in range(extra):
        world["catalog"].save(
            "endpoint",
            dict(url=f"https://jose.example/f{n}.xml", kind="feed", model="rss"),
            source_id=1,
        )
    configure(world["health"], "active", mode="probe", workers=8)


def test_a_small_site_is_read_one_link_at_a_time(world):
    many_links(world)
    reader = Concurrent(rss(1))
    health = world["health"]
    health.create("active")
    worker = HealthWorker(health, old_news, probe_reader=reader)
    worker.recover()
    worker.execute_run(worker.next_run())
    assert reader.max_seen == 1 and len(reader.asked) == 6


def test_a_site_with_many_links_gets_a_few_reads_at_a_time_but_never_more(world):
    many_links(world)
    reader = Concurrent(rss(1))
    health = world["health"]
    run = health.create("active")
    worker = HealthWorker(health, old_news, probe_reader=reader)
    worker.big_host_links, worker.big_host_slots = 3, 3
    worker.recover()
    worker.execute_run(worker.next_run())
    assert reader.max_seen == 3 and len(reader.asked) == 6
    assert detail(health, run)["total"] == 6


# --- transporte -------------------------------------------------------------------------------


class ByTransportProbe:
    """Sonda cuja resposta depende do transporte gravado no link."""

    def __init__(self, answers: dict[str, Any], default: Any) -> None:
        self.answers, self.default = answers, default

    def __call__(self, link: dict[str, Any]) -> Any:
        probe, transport = self, link.get("access_transport", "direct")

        class One:
            def get(self, url: str) -> FetchResult:
                answer = probe.answers.get(transport, probe.default)
                if isinstance(answer, FetchResult):
                    return answer
                return FetchResult(url, 200, answer, None, url)

        return One()


CYCLE = FetchResult("u", None, b"", "redirect_cycle: ciclo de redirecionamento")


def test_a_link_refused_by_its_transport_is_kept_and_moved_to_the_one_that_works(world):
    health = world["health"]
    run_probe(health, ByTransportProbe({"curl_chrome_h1": rss(1)}, CYCLE))
    row = state_row(health)
    assert link_row(health)["active"] and row
    assert (row["health_category"], row["health_failures"]) == ("healthy", 0)
    assert link_row(health)["access_transport"] == "curl_chrome_h1"
    assert "health_transport" in events(health)


def test_a_link_no_transport_can_open_is_still_inactivated_at_once(world):
    health = world["health"]
    run_probe(health, ByTransportProbe({}, CYCLE))
    row = state_row(health)
    assert not link_row(health)["active"] and row and row["health_category"] == "access_error"
    assert link_row(health)["access_transport"] == "direct"


def test_changing_the_transport_resets_the_proof_that_belonged_to_the_old_one(world):
    health, catalog = world["health"], world["catalog"]
    link = catalog.endpoint(1)
    catalog.save("endpoint", dict(version=link["version"], read_order="newest_first"), 1)
    set_state(health, 1, last_test=dict(model_version=1, usable_for_news=True))
    run_probe(health, ByTransportProbe({"curl_chrome_h1": rss(1)}, CYCLE))
    row = state_row(health)
    assert link_row(health)["read_order"] == "unknown"
    assert row and not row["last_test"] and not row["evidence"]


def test_an_inactive_link_is_recovered_with_the_transport_that_passes_the_full_test(
    world, monkeypatch
):
    from buscador_midias.catalog import testing

    real = testing.run_test

    def by_transport(config, source, progress, factory):
        ok = config["access_transport"] == "curl_chrome_h1"
        return real(config, source, progress, old_news if ok else (lambda r, p: FakeReader({}, p)))

    monkeypatch.setattr("buscador_midias.db.health_worker.run_test", by_transport)
    run = inactive_run(
        world["health"], ByTransportProbe({"curl_chrome_h1": rss(2)}, CYCLE), world["catalog"]
    )
    assert world["catalog"].endpoint(1)["active"]
    assert link_row(world["health"])["access_transport"] == "curl_chrome_h1"
    assert detail(world["health"], run)["items"][0]["activated"]


def test_the_frozen_snapshot_carries_the_transport_of_the_link(world):
    """A rotina antiga perdia o transporte (sempre 'direct'); agora ele chega ao teste."""
    health, catalog = world["health"], world["catalog"]
    link = catalog.endpoint(1)
    catalog.save("endpoint", dict(version=link["version"], access_transport="curl_chrome_h2"), 1)
    seen: list[str] = []

    def spy(config, source, progress, factory):
        seen.append(config["access_transport"])
        return dict(usable_for_news=True)

    health.create("active")
    with patch("buscador_midias.db.health_worker.run_test", spy):
        execute(health)
    assert seen == ["curl_chrome_h2"]
