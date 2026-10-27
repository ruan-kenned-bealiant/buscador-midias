"""Empréstimo de capacidade entre filas, sem conexão ao RabbitMQ."""

import json
from collections import defaultdict, deque
from types import SimpleNamespace

import pytest

from buscador_midias.ingestion import broker
from buscador_midias.ingestion.runtime import Service


class Channel:
    def __init__(self):
        self.messages = defaultdict(deque)
        self.reads = []
        self.acks = []

    def basic_get(self, *, queue, auto_ack):
        assert auto_ack is False
        self.reads.append(queue)
        return self.messages[queue].popleft() if self.messages[queue] else (None, None, None)

    def add(self, stage, *, recovery=False):
        self.messages[broker.queue_name(stage, recovery=recovery)].append(
            (SimpleNamespace(delivery_tag=17), None, json.dumps({"job_id": "a" * 32}).encode())
        )

    def basic_ack(self, tag):
        self.acks.append(tag)


@pytest.mark.parametrize("stage", ["discovery", "fetch", "context", "evidence", "persist", "dedup"])
def test_new_work_is_preferred_to_recovery(stage):
    channel = Channel()
    channel.add(stage)
    channel.add(stage, recovery=True)
    method, _, _, recovery = Service.take_message(channel, stage)
    assert method and recovery is False
    assert channel.reads == [broker.queue_name(stage)]


def test_dedicated_recovery_worker_does_not_take_primary_messages():
    channel = Channel()
    channel.add("fetch")
    method, _, _, recovery = Service.take_message(channel, "fetch", recovery=True)
    assert method is None and recovery is True
    assert channel.reads == [broker.queue_name("fetch", recovery=True)]


@pytest.mark.parametrize("stage", ["discovery", "dedup"])
def test_idle_primary_worker_processes_recovery_on_correct_lane(monkeypatch, stage):
    channel = Channel()
    channel.add(stage, recovery=True)
    connection = SimpleNamespace(is_open=True, channel=lambda: channel, close=lambda: None)
    monkeypatch.setattr(broker, "connect", lambda: connection)
    monkeypatch.setattr(broker, "declare", lambda _: None)
    repository = SimpleNamespace(capacity=lambda _: (True, ""))
    service = Service(repository, engine=object())
    service.configuration = lambda: {"workers_enabled": True, "workers": {stage: 9}}
    processed = []

    def process(ident, owner, *args, recovery):
        processed.append((ident, recovery))
        service.stop.set()

    monkeypatch.setattr(service, "process", process)
    monkeypatch.setattr(service, "process_many", process)
    service.worker(stage, 0)
    expected = ["a" * 32] if stage == "dedup" else "a" * 32
    assert processed == [(expected, True)]
    assert channel.acks == [17]
    assert channel.reads[:2] == [broker.queue_name(stage), broker.queue_name(stage, recovery=True)]
    assert all(q == broker.queue_name(stage, recovery=True) for q in channel.reads[2:])


def test_capacity_is_rechecked_quickly_and_shared_by_workers(monkeypatch):
    from buscador_midias.ingestion import runtime

    clock = [10.0]
    allowed = [False]
    calls = []

    def capacity(stage):
        calls.append(stage)
        return allowed[0], "fila cheia" if not allowed[0] else ""

    service = Service(SimpleNamespace(capacity=capacity), engine=object())
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    for _ in range(18):
        assert service.capacity("context")[0] is False
    assert calls == ["context"]
    allowed[0] = True
    clock[0] += 0.5
    assert service.capacity("context") == (True, "")
    assert calls == ["context", "context"]


def test_concurrent_workers_share_one_capacity_query():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(18)
    calls = []

    def capacity(stage):
        calls.append(stage)
        return True, ""

    service = Service(SimpleNamespace(capacity=capacity), engine=object())

    def check(_):
        barrier.wait(timeout=5)
        return service.capacity("fetch")

    with ThreadPoolExecutor(max_workers=18) as pool:
        assert list(pool.map(check, range(18))) == [(True, "")] * 18
    assert calls == ["fetch"]


def test_capacity_failure_is_not_cached_and_other_stages_are_independent():
    calls = []

    def capacity(stage):
        calls.append(stage)
        if len(calls) == 1:
            raise OSError("banco indisponivel")
        return stage == "persist", ""

    service = Service(SimpleNamespace(capacity=capacity), engine=object())
    with pytest.raises(OSError):
        service.capacity("context")
    assert service.capacity("context") == (False, "")
    assert service.capacity("persist") == (True, "")
    assert calls == ["context", "context", "persist"]


def test_evidence_uses_saved_html_without_opening_network_or_database(tmp_path):
    from buscador_midias.evidence.storage import LocalEvidenceStore
    from buscador_midias.ingestion.artifacts import Artifacts
    from buscador_midias.ingestion.engine import Engine

    key = "a" * 64
    artifacts = Artifacts(tmp_path / "work")
    paragraph = (
        "João Silva explicou as medidas da empresa e apresentou os documentos à reportagem. "
    )
    body = (
        '<html><head><meta charset="utf-8"><title>Empresa apresenta medidas anunciadas</title>'
        '<meta property="og:type" content="article">'
        '<meta property="article:published_time" content="2026-10-26T12:00:00Z">'
        "</head><body><article><h1>Empresa apresenta medidas anunciadas</h1>"
        + "".join(f"<p>{paragraph}</p>" for _ in range(30))
        + "</article></body></html>"
    ).encode()
    artifacts.put(key, body)

    def forbidden(*args):
        pytest.fail("A evidência reaproveitada não deve abrir leitor nem conexão PG")

    engine = Engine(
        SimpleNamespace(connection=forbidden),
        artifacts=artifacts,
        reader_factory=forbidden,
        evidence_store=LocalEvidenceStore(tmp_path / "evidence"),
    )
    output = engine.evidence(
        dict(
            hash=key,
            attempts=1,
            snapshot=dict(source=dict(name="Exemplo", url="https://example.com")),
            article=dict(
                url="https://example.com/noticia",
                payload=dict(
                    title="Empresa apresenta medidas anunciadas",
                    content_type="text/html",
                    analysis=dict(entities=[dict(name="João Silva")], finding=False),
                ),
            ),
            run=dict(config=dict(evidence_paid_adverse_only=True, max_attempts=3)),
        )
    )
    assert output["outcome"] == "evidencia_capturada"
    assert output["article"]["evidence"]["provider"] == "direct"
    assert output["metrics"]["requests"] == 0


def test_context_capacity_does_not_query_database_for_evidence_backlog():
    from buscador_midias.ingestion.store import IngestStore

    repository = IngestStore(SimpleNamespace())

    def forbidden():
        pytest.fail("O contexto não deve esperar nem consultar o backlog de evidências")

    repository.connection = forbidden
    assert repository.capacity("context") == (True, "")
