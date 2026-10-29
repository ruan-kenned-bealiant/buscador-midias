"""Filas, retomada, configurações e testes reais em PostgreSQL isolado."""

import os
import threading
from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb
from test_catalog_postgres import local, schema  # noqa: F401
from test_catalog_testing import FakeReader, article

from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.health_worker import HealthWorker
from buscador_midias.catalog.migrate_postgres import migrate_catalog
from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.catalog.store import CatalogError
from buscador_midias.net.fetcher import FetchResult

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.fixture
def health(local, schema):  # noqa: F811
    migrate_catalog(local.path, schema)
    repository = HealthStore(PostgresCatalogStore(schema))
    repository.initialize()
    return repository


def old_news(roots, progress):
    root = roots[0].rstrip("/")
    return FakeReader(
        {
            roots[1]: (
                f"<rss><channel><item><link>{root}/noticia</link>"
                "<pubDate>2001-01-01</pubDate></item></channel></rss>"
            ).encode(),
            root + "/noticia": FetchResult(
                root + "/noticia", 200, article(), None, root + "/noticia", "text/html"
            ),
        },
        progress,
    )


class FreshProbe:
    """Sonda leve com notícia de ontem: o portão de frescor libera a reativação."""

    def __call__(self, link):
        return self

    def get(self, url):
        from datetime import timedelta

        day = (datetime.now(UTC) - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        body = (
            f"<rss><channel><item><link>{url}n</link><pubDate>{day}</pubDate></item>"
            "</channel></rss>"
        )
        return FetchResult(url, 200, body.encode(), None, url)


def execute(health, factory=old_news, probe=None):
    worker = HealthWorker(health, factory, probe_reader=probe or FreshProbe())
    worker.recover()
    run = worker.next_run()
    worker.execute_run(run)
    return run


def test_active_run_reuses_real_test_ignores_date_and_keeps_catalog_untouched(health):
    before = health.store.endpoint(1)
    run = health.create("active")
    assert run["total"] == 1
    with pytest.raises(CatalogError):
        health.create("active")
    execute(health)
    detail = health.details(run["id"])
    assert detail["run"]["status"] == "completed"
    row = detail["items"][0]
    assert row["category"] == "healthy" and row["attempts"] == 1
    assert row["result"]["text_samples"] == 1
    assert row["result"]["parameters"]["lookback_days"] == 0
    assert health.store.endpoint(1) == before
    assert health.overview()["runs"][0]["counts"] == {"healthy": 1}


def test_recovery_excludes_manual_inactive_audio_deleted_and_includes_access_failure(health):
    with health.connection() as db:
        failed = dict(
            url="https://jose.example/feed/",
            source_url="https://jose.example",
            model="rss",
            model_version=1,
            endpoint_accessible=True,
            samples=[dict(accessed=False, status=403)],
        )
        db.execute("UPDATE links SET active=0,last_test=%s WHERE id=1", (Jsonb(failed),))
    audio = health.store.save(
        "endpoint", dict(url="https://jose.example/audio", kind="feed", active=False), source_id=1
    )
    manual = health.store.save(
        "endpoint", dict(url="https://jose.example/manual", kind="feed", active=False), source_id=1
    )
    deleted = health.store.save(
        "endpoint", dict(url="https://jose.example/deleted", kind="feed", active=False), source_id=1
    )
    health.store.delete("endpoint", deleted["id"], deleted["version"])
    run = health.create("recovery")
    assert run["total"] == 1
    execute(health)
    assert health.details(run["id"])["items"][0]["category"] == "healthy"
    assert health.store.endpoint(1)["active"] is False
    assert health.create("recovery")["total"] == 0  # Recuperado não é rechecado repetidamente.
    assert health.store.endpoint(audio["id"])["active"] is False
    assert health.store.endpoint(manual["id"])["active"] is False


def test_limited_retries_and_persisted_failures(health):
    calls = []

    def blocked(config, source, progress, factory):
        calls.append(config["test_id"])
        return dict(endpoint_accessible=False, endpoint_status=403, outcome="blocked")

    from unittest.mock import patch

    run = health.create("active")
    with patch("buscador_midias.catalog.health_worker.run_test", blocked):
        execute(health)
    row = health.details(run["id"])["items"][0]
    assert len(calls) == 2 and row["attempts"] == 2
    assert len(row["history"]) == 2 and row["category"] == "access_error"
    link = health.store.endpoint(1)
    health.store.save("endpoint", dict(version=link["version"], active=False), 1)
    assert health.create("recovery")["total"] == 1


def age_failed_link(health, inactivated_days_ago, checked_days_ago, access_error=True):
    """Deixa o link 1 inativo; ``inactivated_days_ago`` dá a idade da inativação pela saúde."""
    from unittest.mock import patch

    def blocked(config, source, progress, factory):
        return dict(endpoint_accessible=False, endpoint_status=403, outcome="blocked")

    health.create("active")
    with patch("buscador_midias.catalog.health_worker.run_test", blocked):
        execute(health)
    link = health.store.endpoint(1)
    health.store.save("endpoint", dict(version=link["version"], active=False), 1)
    with health.connection() as db:
        db.execute(
            "UPDATE link_health SET checked_at=now()-make_interval(days=>%s) WHERE link_id=1",
            (checked_days_ago,),
        )
        db.execute("DELETE FROM events WHERE action='health_inactivate'")
        if inactivated_days_ago is not None:
            db.execute(
                "INSERT INTO events(entity,entity_id,action,happened_at,before_json,after_json) "
                "VALUES('endpoint',1,'health_inactivate',now()-make_interval(days=>%s),'{}','{}')",
                (inactivated_days_ago,),
            )
        db.execute(
            "UPDATE health_settings SET enabled=true,next_run_at=now()-interval '1 minute' "
            "WHERE kind='recovery'"
        )


@pytest.mark.parametrize(
    ("inactivated_days_ago", "checked_days_ago", "included"),
    [(1, 1, 1), (30, 1, 0), (None, 1, 0), (None, 8, 1)],
)
def test_scheduled_recovery_skips_old_inactives_checked_this_week(
    health, inactivated_days_ago, checked_days_ago, included
):
    age_failed_link(health, inactivated_days_ago, checked_days_ago)
    assert health.create("recovery", "scheduled")["total"] == included


@pytest.mark.parametrize("scope", ["errors", "inactive"])
def test_the_weekly_rule_applies_to_both_scopes_of_the_scheduled_routine(health, scope):
    age_failed_link(health, None, 1)
    with health.connection() as db:
        db.execute("UPDATE health_settings SET scope=%s WHERE kind='recovery'", (scope,))
    assert health.create("recovery", "scheduled")["total"] == 0


def test_a_manual_run_always_tests_everything_in_its_scope(health):
    age_failed_link(health, None, 1)
    assert health.create("recovery")["total"] == 1  # "Executar agora" não é filtrada


def test_resume_cancel_and_catalog_edit_conflicts(health):
    run = health.create("active")
    worker = HealthWorker(health, old_news)
    worker.next_run()
    with health.connection() as db:
        db.execute(
            "UPDATE health_results SET state='running',attempts=1 WHERE run_id=%s", (run["id"],)
        )
    replacement = HealthWorker(health, old_news)
    replacement.recover()
    with health.connection() as db:
        row = db.execute(
            "SELECT state,attempts FROM health_results WHERE run_id=%s", (run["id"],)
        ).fetchone()
        assert row == dict(state="pending", attempts=0)
    health.cancel(run["id"])
    replacement.execute_run(replacement.next_run())
    assert health.details(run["id"])["run"]["status"] == "cancelled"
    run2 = health.create("active")
    link = health.store.endpoint(1)
    health.store.save("endpoint", dict(version=link["version"], search_interval_seconds=900), 1)
    execute(health)
    assert health.details(run2["id"])["items"][0]["state"] == "skipped"


def test_settings_validation_schedule_and_no_overlap(health):
    settings = health.settings()[0]
    assert settings["enabled"] and settings["next_run_at"] > datetime.now(UTC)
    data = {
        k: settings[k]
        for k in ("enabled", "interval_minutes", "workers", "max_attempts", "version")
    }
    data["interval_minutes"] = 30000  # Frequência da saúde não tem teto de quatro horas.
    updated = health.configure(settings["kind"], data)
    assert updated["interval_minutes"] == 30000
    with pytest.raises(CatalogError):
        health.configure(settings["kind"], data)
    data["version"] = updated["version"]
    data["workers"] = True
    with pytest.raises(CatalogError):
        health.configure(settings["kind"], data)
    assert health.create("active", "scheduled") is None
    with health.connection() as db:
        db.execute(
            "UPDATE health_settings SET next_run_at=now()-interval '1 minute' WHERE kind='active'"
        )
    assert health.create("active", "scheduled")["trigger"] == "scheduled"
    assert health.create("active", "scheduled") is None


def test_one_test_per_host_and_multiple_hosts_in_parallel(health, monkeypatch):
    health.store.save("endpoint", dict(url="https://jose.example/second", kind="feed"), source_id=1)
    source = health.store.save("source", dict(name="Outro", url="https://other.example"))
    health.store.save(
        "endpoint", dict(url="https://other.example/feed/", kind="feed"), source_id=source["id"]
    )
    running = set()
    lock = threading.Lock()
    overlaps = []
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

    monkeypatch.setattr("buscador_midias.catalog.health_worker.run_test", check)
    health.create("active")
    execute(health)
    assert max(overlaps) == 2
    assert health.overview()["runs"][0]["counts"] == {"healthy": 3}


def test_health_api_enforces_authorization_and_worker_executes(health, local, monkeypatch):  # noqa: F811
    import time

    from test_catalog_manager import request

    import buscador_midias.dashboard.server as server_module

    monkeypatch.setattr(server_module, "open_catalog", lambda *args, **kwargs: health.store)
    server = server_module.make_server(
        lambda: None,
        "127.0.0.1",
        0,
        sites_path=local.sites,
        functional_directory=local.snapshot.parent,
        test_reader_factory=old_news,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert request(url, "/saude")[0] == 200
        assert request(url, "/assets/health.js")[0] == 200
        base = "/api/gerenciador"
        _, summary = request(url, base + "/resumo")
        headers = {"X-Catalog-Token": summary["csrf_token"]}
        assert request(url, base + "/saude/rodadas", "POST", dict(kind="active"))[0] == 403
        code, run = request(url, base + "/saude/rodadas", "POST", dict(kind="active"), headers)
        assert code == 202
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            _, detail = request(url, base + "/saude/rodadas/" + run["id"])
            if detail["run"]["status"] == "completed":
                break
            time.sleep(0.1)
        assert detail["run"]["status"] == "completed"
        assert detail["items"][0]["category"] == "healthy"
        _, overview = request(url, base + "/saude")
        setting = overview["settings"][0]
        data = {
            k: setting[k]
            for k in ("enabled", "interval_minutes", "workers", "max_attempts", "version")
        }
        data["enabled"] = False
        assert (
            request(url, base + "/saude/configuracoes/" + setting["kind"], "PATCH", data, headers)[
                0
            ]
            == 200
        )
        assert (
            request(url, base + "/saude/configuracoes/" + setting["kind"], "PATCH", data, headers)[
                0
            ]
            == 409
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_scheduler_runs_due_rounds_once_with_two_workers(health):
    import time

    with health.connection() as db:
        db.execute("UPDATE health_settings SET next_run_at=now()-interval '1 hour'")
    first = HealthWorker(health, old_news)
    second = HealthWorker(health, old_news)
    first.start()
    second.start()
    try:
        deadline = time.monotonic() + 12
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


def test_explicit_scan_inactive_activates_textual_proof_and_archives_test(health):
    link = health.store.endpoint(1)
    health.store.save("endpoint", dict(version=link["version"], active=False), 1)
    run = health.create("recovery", options=dict(scope="inactive", activate_recovered=True))
    assert run["total"] == 1
    execute(health)
    row = health.details(run["id"])["items"][0]
    assert row["activated"] and row["category"] == "healthy"
    updated = health.store.endpoint(1)
    assert updated["active"] and updated["last_test"]["usable_for_news"]
    with health.connection() as db:
        assert db.execute(
            "SELECT 1 FROM events WHERE entity_id=1 AND action='health_reactivate'"
        ).fetchone()
        assert db.execute("SELECT 1 FROM link_test_results WHERE link_id=1").fetchone()


def test_explicit_recovery_does_not_activate_audio_empty_or_manually_inactive_site(
    health, monkeypatch
):
    link = health.store.endpoint(1)
    health.store.save("endpoint", dict(version=link["version"], active=False), 1)

    def no_news(config, source, progress, factory):
        return dict(usable_for_news=False, outcome="empty", endpoint_accessible=True)

    monkeypatch.setattr("buscador_midias.catalog.health_worker.run_test", no_news)
    run = health.create("recovery", options=dict(scope="inactive", activate_recovered=True))
    execute(health)
    assert not health.store.endpoint(1)["active"]
    assert health.details(run["id"])["items"][0]["category"] == "no_news"


def auto_exclude(health, ident=1):
    """Simula a decisão auditada da validação histórica, sem remover a evidência."""
    with health.connection() as db:
        before = db.execute("SELECT * FROM links WHERE id=%s", (ident,)).fetchone()
        after = db.execute(
            "UPDATE links SET active=0,deleted_at=now(),version=version+1,"
            "last_test=%s WHERE id=%s RETURNING *",
            (Jsonb(dict(endpoint_accessible=False, warnings=["HTTP 403"])), ident),
        ).fetchone()
        db.execute(
            "INSERT INTO events(entity,entity_id,action,happened_at,before_json,after_json) "
            "VALUES('endpoint',%s,'validation_exclude',now(),%s,%s)",
            (ident, Jsonb(health.jsonable(before)), Jsonb(health.jsonable(after))),
        )


def test_auto_excluded_restores_only_proven_links_and_automatic_parent(health):
    other = health.store.save(
        "endpoint", dict(url="https://jose.example/other", kind="feed"), source_id=1
    )
    auto_exclude(health)
    auto_exclude(health, other["id"])
    with health.connection() as db:
        before = db.execute("SELECT * FROM sites WHERE id=1").fetchone()
        after = db.execute(
            "UPDATE sites SET active=0,version=version+1 WHERE id=1 RETURNING *"
        ).fetchone()
        db.execute(
            "INSERT INTO events(entity,entity_id,action,happened_at,before_json,after_json) "
            "VALUES('source',1,'validation_inactivate',now(),%s,%s)",
            (Jsonb(health.jsonable(before)), Jsonb(health.jsonable(after))),
        )
    run = health.create(
        "recovery", options=dict(scope="auto_excluded_access", activate_recovered=True)
    )
    assert run["total"] == 2
    execute(health)
    assert health.store.source(1)["active"]
    assert health.store.endpoint(1)["active"]
    assert health.store.endpoint(other["id"])["active"]
    assert all(i["activated"] for i in health.details(run["id"])["items"])
    with health.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) n FROM events WHERE action='validation_exclude'"
            ).fetchone()["n"]
            == 2
        )
        assert (
            db.execute(
                "SELECT count(*) n FROM events WHERE action='health_restore_site'"
            ).fetchone()["n"]
            == 1
        )


@pytest.mark.parametrize("manual", ["link", "source", "edit", "audio"])
def test_auto_excluded_preserves_manual_actions_and_nonaccess_failures(health, manual):
    auto_exclude(health)
    with health.connection() as db:
        if manual == "link":
            db.execute(
                "INSERT INTO events(entity,entity_id,action,happened_at,after_json) "
                "VALUES('endpoint',1,'delete',now(),%s)",
                (Jsonb(dict(version=2)),),
            )
        elif manual == "source":
            db.execute("UPDATE sites SET active=0,version=version+1 WHERE id=1")
        elif manual == "edit":
            db.execute("UPDATE links SET version=version+1 WHERE id=1")
        else:
            db.execute(
                "UPDATE links SET last_test=%s WHERE id=1",
                (Jsonb(dict(samples=[dict(accessed=True)])),),
            )
    run = health.create(
        "recovery", options=dict(scope="auto_excluded_access", activate_recovered=True)
    )
    assert run["total"] == 0


def test_auto_excluded_failure_remains_deleted(health):
    auto_exclude(health)
    run = health.create(
        "recovery", options=dict(scope="auto_excluded_access", activate_recovered=True)
    )
    execute(health, lambda roots, progress: FakeReader({}, progress))
    assert not health.details(run["id"])["items"][0]["activated"]
    with pytest.raises(CatalogError):
        health.store.endpoint(1)


def test_auto_excluded_manual_change_during_io_is_preserved(health):
    auto_exclude(health)
    run = health.create(
        "recovery", options=dict(scope="auto_excluded_access", activate_recovered=True)
    )

    def changed(roots, progress):
        with health.connection() as db:
            db.execute("UPDATE links SET version=version+1 WHERE id=1")
        return old_news(roots, progress)

    execute(health, changed)
    assert health.details(run["id"])["items"][0]["state"] == "skipped"
    with pytest.raises(CatalogError):
        health.store.endpoint(1)
