"""Transporte do novo desenho: nomes de fila, mensagem mínima e validação do que chega."""

from __future__ import annotations

import json
from typing import Any

import pytest

from buscador_midias.pipeline import broker


class FakeChannel:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.declared: list[dict[str, Any]] = []
        self.qos: int | None = None
        self.confirms = False

    def queue_declare(self, **kwargs: Any) -> Any:
        self.declared.append(kwargs)

        class Result:
            class method:  # noqa: N801
                message_count = 7
                consumer_count = 2

        return Result

    def basic_publish(self, **kwargs: Any) -> None:
        self.published.append(kwargs)

    def confirm_delivery(self) -> None:
        self.confirms = True

    def basic_qos(self, prefetch_count: int) -> None:
        self.qos = prefetch_count


def test_queue_names_use_the_prefix_stage_and_lane(monkeypatch):
    monkeypatch.delenv("BM_QUEUE_PREFIX", raising=False)
    assert broker.queue_name("fetch") == "bm.fetch"
    assert broker.queue_name("fetch", "recovery") == "bm.fetch.recovery"
    monkeypatch.setenv("BM_QUEUE_PREFIX", "teste.x")
    assert broker.queue_name("persist") == "teste.x.persist"


@pytest.mark.parametrize(("stage", "lane"), [("export", "primary"), ("fetch", "turbo")])
def test_unknown_stage_or_lane_is_rejected(stage, lane):
    with pytest.raises(ValueError):
        broker.queue_name(stage, lane)


def test_declare_creates_twelve_durable_queues_and_no_failed_queue():
    channel = FakeChannel()
    broker.declare(channel)
    names = [d["queue"] for d in channel.declared]
    assert len(names) == 12 and len(set(names)) == 12
    assert not any(n.endswith(".failed") for n in names)
    assert all(d["durable"] for d in channel.declared)
    assert channel.confirms


def test_the_message_carries_only_the_stage_and_the_key():
    channel = FakeChannel()
    broker.publish(channel, "fetch", "ab" * 32)
    broker.publish(channel, "discovery", 42, "recovery")
    first, second = channel.published
    assert json.loads(first["body"]) == {"s": "fetch", "k": "ab" * 32}
    assert json.loads(second["body"]) == {"s": "discovery", "k": 42}
    assert first["routing_key"] == "bm.fetch" and second["routing_key"] == "bm.discovery.recovery"
    assert first["properties"].delivery_mode == 2 and first["mandatory"] is True


def test_decode_returns_stage_and_key():
    assert broker.decode(json.dumps({"s": "fetch", "k": "a" * 64}).encode()) == ("fetch", "a" * 64)
    assert broker.decode(json.dumps({"s": "discovery", "k": 9}).encode()) == ("discovery", 9)


@pytest.mark.parametrize(
    "body",
    [
        b"nao-json",
        b"[]",
        json.dumps({"s": "fetch"}).encode(),
        json.dumps({"k": "x"}).encode(),
        json.dumps({"s": "export", "k": "x"}).encode(),
        json.dumps({"s": "fetch", "k": ["x"]}).encode(),
        json.dumps({"s": "fetch", "k": ""}).encode(),
        json.dumps({"s": "discovery", "k": True}).encode(),
    ],
)
def test_decode_rejects_malformed_messages(body):
    with pytest.raises(ValueError):
        broker.decode(body)


def test_depth_reports_messages_and_consumers_of_a_queue():
    assert broker.depth(FakeChannel(), "fetch") == (7, 2)
