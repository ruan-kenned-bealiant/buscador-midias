"""``Pipeline`` com motor e canal de mentira: o que acontece com cada mensagem e cada erro."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg import OperationalError
from psycopg.types.json import Jsonb

from buscador_midias.db.settings import SettingsRepository
from buscador_midias.db.store import WorkStore
from buscador_midias.ingestion.engine import (
    HostBusyError,
    ProviderUnavailableError,
    RetryableError,
)
from buscador_midias.pipeline import broker
from buscador_midias.pipeline.service import Pipeline

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

SINCE = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
UNTIL = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)


class FakeChannel:
    def __init__(self, ready: int = 0, fail: bool = False) -> None:
        self.ready, self.fail, self.published = ready, fail, []

    def queue_declare(self, **_: Any) -> Any:
        ready = self.ready

        class Result:
            class method:  # noqa: N801
                message_count = ready
                consumer_count = 0

        return Result

    def basic_publish(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("broker caiu")
        self.published.append(json.loads(kwargs["body"]))


class FakeRoutes:
    def outside(self, *_: Any) -> None:
        return None


class FakeEngine:
    def __init__(self) -> None:
        self.output: dict[str, Any] = dict(next="fetch")
        self.error: Exception | None = None
        self.routes = FakeRoutes()
        self.calls = 0
        self.delay = 0.0

    def execute(self, job: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.output

    def dedup_many(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.error:
            raise self.error
        return [self.output for _ in jobs]


def digest(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


@pytest.fixture
def world(schema_name: str):
    store = WorkStore(schema_name)
    store.install()
    with store.connection() as db:
        db.execute(
            "INSERT INTO bm_sites(identity,name,url) VALUES('a.example','A','https://a.example')"
        )
        db.execute(
            "INSERT INTO bm_links(site_id,url,kind,mechanism) "
            "VALUES(1,'https://a.example/feed','feed','rss')"
        )
        db.execute(
            "INSERT INTO bm_runs(id,type,trigger,status,config,since,until) "
            "VALUES('r1','search','scheduled','running',%s,%s,%s)",
            (Jsonb(dict(max_attempts=3, retry_minutes=1)), SINCE, UNTIL),
        )
    engine = FakeEngine()
    return store, engine, Pipeline(store, engine, lease_interval=0.05)  # type: ignore[arg-type]


def article(store: WorkStore, name: str, **columns: Any) -> str:
    values: dict[str, Any] = dict(
        hash=digest(name),
        url=f"https://a.example/{name}",
        host="a.example",
        site_id=1,
        link_id=1,
        first_run_id="r1",
        run_id="r1",
    )
    values.update(columns)
    names = ",".join(values)
    marks = ",".join(["%s"] * len(values))
    with store.connection() as db:
        db.execute(f"INSERT INTO bm_articles({names}) VALUES({marks})", tuple(values.values()))
    return str(values["hash"])


def row(store: WorkStore, key: str) -> dict[str, Any]:
    with store.connection() as db:
        return db.execute("SELECT * FROM bm_articles WHERE hash=%s", (key,)).fetchone()


# --- publicar ----------------------------------------------------------------------------------


def test_publish_sends_the_waiting_keys_and_marks_them_queued(world):
    store, _, pipeline = world
    keys = [article(store, f"n{n}") for n in range(3)]
    channel = FakeChannel()
    assert pipeline.publish_once(channel, "dedup") == 3
    assert {m["k"] for m in channel.published} == set(keys)
    assert {m["s"] for m in channel.published} == {"dedup"}
    assert {row(store, k)["state"] for k in keys} == {"queued"}


def test_publish_stops_when_the_queue_has_no_room(world):
    store, _, pipeline = world
    article(store, "n1")
    assert pipeline.publish_once(FakeChannel(ready=10_000), "dedup") == 0
    assert row(store, digest("n1"))["state"] == "pending"


def test_a_broker_failure_leaves_nothing_marked_as_queued(world):
    store, _, pipeline = world
    key = article(store, "n1")
    with pytest.raises(RuntimeError):
        pipeline.publish_once(FakeChannel(fail=True), "dedup")
    assert row(store, key)["state"] == "pending"


def test_the_recovery_lane_publishes_only_its_own_articles_and_few_at_a_time(world):
    store, _, pipeline = world
    primary = article(store, "p")
    recovery = [article(store, f"r{n}", lane="recovery") for n in range(8)]
    channel = FakeChannel()
    count = pipeline.publish_once(channel, "dedup", "recovery")
    assert count == pipeline.configuration()["workers"]["dedup"] == 1
    assert row(store, primary)["state"] == "pending"
    assert sum(row(store, k)["state"] == "queued" for k in recovery) == 1


def test_discovery_publication_respects_the_per_host_limit(world):
    store, _, pipeline = world
    with store.connection() as db:
        for n in range(4):
            db.execute(
                "INSERT INTO bm_run_links(run_id,link_id) VALUES('r1',1) ON CONFLICT DO NOTHING"
            )
            db.execute(
                "INSERT INTO bm_discovery_tasks(run_id,link_id,url,host) "
                "VALUES('r1',1,%s,'a.example')",
                (f"https://a.example/s{n}.xml",),
            )
    channel = FakeChannel()
    assert pipeline.publish_once(channel, "discovery") == 1  # host_concurrency padrão = 1


# --- processar ---------------------------------------------------------------------------------


def run_one(world, error: Exception | None = None, output: dict[str, Any] | None = None) -> str:
    store, engine, pipeline = world
    key = article(store, "n1")
    engine.error = error
    if output is not None:
        engine.output = output
    assert pipeline.process("dedup", key, "w1")
    return key


def test_success_confirms_the_result(world):
    store, engine, _ = world
    key = run_one(world, output=dict(next="fetch", article=dict(title="T")))
    after = row(store, key)
    assert (after["stage"], after["state"]) == ("fetch", "pending")
    assert after["payload"]["title"] == "T" and engine.calls == 1


def test_a_stale_message_is_not_processed(world):
    store, engine, pipeline = world
    key = article(store, "n1", state="done", outcome="persisted")
    assert pipeline.process("dedup", key, "w1") is False
    assert engine.calls == 0


def test_a_busy_host_defers_without_consuming_an_attempt(world):
    store, _, _ = world
    key = run_one(world, error=HostBusyError("Host aguardando Retry-After", 30))
    after = row(store, key)
    assert (after["state"], after["attempts"]) == ("retry", 0)


def test_a_retryable_error_goes_through_backoff(world):
    store, _, _ = world
    key = run_one(world, error=RetryableError("HTTP 503 no XML", {}))
    after = row(store, key)
    assert (after["state"], after["attempts"], after["lane"]) == ("retry", 1, "primary")


def test_a_non_retryable_block_fails_the_article_with_its_class(world):
    store, _, _ = world
    key = run_one(world, error=RetryableError("HTTP 403 no XML", {}, retryable=False))
    after = row(store, key)
    assert (after["state"], after["reason"], after["reason_class"]) == (
        "failed",
        "http_403",
        "blocked",
    )


def test_a_provider_outage_pauses_the_stage_and_sends_the_work_to_recovery(world):
    store, _, pipeline = world
    key = run_one(world, error=ProviderUnavailableError("OpenRouter indisponível", {}))
    after = row(store, key)
    assert (after["state"], after["lane"], after["attempts"]) == ("retry", "recovery", 0)
    for _ in range(5):
        pipeline.breaker.failure("dedup", "OpenRouter indisponível")
    assert pipeline.should_wait("dedup", 0, "primary")


def test_a_database_blip_defers_the_task(world):
    store, _, _ = world
    key = run_one(world, error=OperationalError("conexão perdida"))
    assert row(store, key)["state"] == "retry"


def test_an_unexpected_exception_is_an_internal_failure_without_leaking_details(world):
    store, _, _ = world
    key = run_one(world, error=KeyError("segredo-na-mensagem"))
    after = row(store, key)
    assert after["state"] == "retry"
    assert "segredo" not in json.dumps(after["history"], default=str)
    assert "KeyError" in after["history"][-1]["detail"]


def test_a_long_stage_keeps_renewing_its_lease(world):
    store, engine, pipeline = world
    key = article(store, "n1")
    engine.delay = 0.35
    seen: list[Any] = []
    original = store.renew_lease

    def spy(job: dict[str, Any]) -> bool:
        seen.append(job["hash"])
        return original(job)

    store.renew_lease = spy  # type: ignore[method-assign]
    assert pipeline.process("dedup", key, "w1")
    assert len(seen) >= 3


def test_the_dedup_batch_finishes_in_one_go(world):
    store, engine, pipeline = world
    keys = [article(store, f"n{n}") for n in range(5)]
    engine.output = dict(status="known", article_id=9)
    assert pipeline.process_many(keys, "w1") == 5
    assert {row(store, k)["outcome"] for k in keys} == {"known"}


def test_a_failed_batch_marks_every_article_for_retry(world):
    store, engine, pipeline = world
    keys = [article(store, f"n{n}") for n in range(3)]
    engine.error = RuntimeError("explodiu")
    assert pipeline.process_many(keys, "w1") == 3
    assert {row(store, k)["state"] for k in keys} == {"retry"}


# --- mensagens ---------------------------------------------------------------------------------


def test_invalid_messages_and_wrong_stage_messages_are_dropped(world):
    store, engine, pipeline = world
    key = article(store, "n1")
    assert pipeline.handle("dedup", b"nao-json", "w1", "primary") is True
    wrong = json.dumps({"s": "fetch", "k": key}).encode()
    assert pipeline.handle("dedup", wrong, "w1", "primary") is True
    assert engine.calls == 0 and row(store, key)["state"] == "pending"
    good = json.dumps({"s": "dedup", "k": key}).encode()
    pipeline.handle("dedup", good, "w1", "primary")
    assert engine.calls == 1


# --- pausas ------------------------------------------------------------------------------------


def test_workers_beyond_the_configured_count_or_with_workers_off_wait(world, schema_name):
    store, _, pipeline = world
    assert pipeline.should_wait("fetch", 0, "primary") is None
    assert pipeline.should_wait("fetch", 5, "primary") == "pausado"
    settings = SettingsRepository(schema_name)
    settings.configure_search(
        dict(version=settings.search()["version"], config=dict(workers_enabled=False))
    )
    pipeline._config_until = 0
    assert pipeline.should_wait("fetch", 0, "primary") == "pausado"


def test_a_full_downstream_stage_throttles_the_stage_before_it(world):
    store, _, pipeline = world
    for n in range(200):
        article(store, f"c{n}", stage="context")
    pipeline._capacity.clear()
    assert pipeline.should_wait("fetch", 0, "primary")


# --- manutenção --------------------------------------------------------------------------------


def test_the_scheduler_tick_recovers_an_expired_lease(world):
    store, _, pipeline = world
    key = article(
        store, "n1", state="running", owner="gone", attempts=1, locked_until="2000-01-01Z"
    )
    pipeline.scheduler_tick()
    assert row(store, key)["state"] == "retry"


def test_the_window_filter_closes_what_the_date_already_excludes(world):
    store, _, pipeline = world
    key = article(
        store,
        "old",
        payload=Jsonb(
            dict(published_hint="2026-10-01T10:00:00+00:00", published_hint_basis="xml_publication")
        ),
    )
    cursor, closed = pipeline.filter_window_once()
    assert closed == 1 and cursor == key
    assert (row(store, key)["state"], row(store, key)["outcome"]) == ("done", "outside_window")
    assert pipeline.filter_window_once(cursor) == ("", 0)


def test_the_stage_and_lane_names_are_stable():
    assert broker.queue_name("persist", "recovery").endswith("persist.recovery")


def test_each_role_gets_a_small_pool_so_the_shared_database_keeps_room(monkeypatch):
    from buscador_midias.pipeline.service import POOL_SIZES, pool_size

    monkeypatch.delenv("BM_POOL_MAX", raising=False)
    assert pool_size("worker", "fetch") == POOL_SIZES["fetch"] == 4
    assert pool_size("worker", "context") == 8 and pool_size("publisher") == 4
    # Sete processos de busca somados ficam longe do teto de 100 conexões do banco.
    assert (
        sum(
            POOL_SIZES[s]
            for s in (
                "discovery",
                "dedup",
                "fetch",
                "context",
                "evidence",
                "persist",
                "publisher",
                "scheduler",
                "window",
            )
        )
        <= 40
    )


def test_the_pool_size_can_be_overridden_and_ignores_garbage(monkeypatch):
    from buscador_midias.pipeline.service import pool_size

    monkeypatch.setenv("BM_POOL_MAX", "3")
    assert pool_size("worker", "context") == 3
    monkeypatch.setenv("BM_POOL_MAX", "muitas")
    assert pool_size("worker", "context") == 8
