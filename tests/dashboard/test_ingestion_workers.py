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
