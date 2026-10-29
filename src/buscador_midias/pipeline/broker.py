"""Transporte RabbitMQ durável; o PostgreSQL é a fonte da verdade e a mensagem só aponta para ela.

Uma fila por etapa e faixa (``primary`` ou ``recovery``): 12 filas. Não há fila de mortos: a falha
de uma notícia fica na própria linha (estado ``failed``, motivo e classe) e é decidida por pessoas.
Mensagem inválida é descartada com registro, nunca reenfileirada.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pika

STAGES = ("discovery", "dedup", "fetch", "context", "evidence", "persist")
LANES = ("primary", "recovery")


def connect() -> Any:
    """Abre uma conexão sem colocar senhas em URLs ou mensagens de erro."""
    return pika.BlockingConnection(
        pika.ConnectionParameters(
            host=os.environ.get("RABBITMQ_HOST", "127.0.0.1"),
            port=int(os.environ.get("RABBITMQ_PORT", "5672")),
            virtual_host=os.environ.get("RABBITMQ_VHOST", "/"),
            credentials=pika.PlainCredentials(
                os.environ.get("RABBITMQ_USER", "guest"),
                os.environ.get("RABBITMQ_PASSWORD", "guest"),
            ),
            heartbeat=300,
            blocked_connection_timeout=30,
            socket_timeout=10,
            connection_attempts=1,
            client_properties={"connection_name": "buscador-midias-pipeline"},
        )
    )


def queue_name(stage: str, lane: str = "primary") -> str:
    """Namespace próprio; testes usam um prefixo descartável em ``BM_QUEUE_PREFIX``."""
    if stage not in STAGES or lane not in LANES:
        raise ValueError("Etapa ou faixa desconhecida")
    prefix = os.environ.get("BM_QUEUE_PREFIX", "bm")
    return f"{prefix}.{stage}" + (".recovery" if lane == "recovery" else "")


def declare(channel: Any) -> None:
    """Cria as 12 filas de trabalho e liga a confirmação de entrega."""
    for stage in STAGES:
        for lane in LANES:
            channel.queue_declare(queue=queue_name(stage, lane), durable=True)
    channel.confirm_delivery()


def publish(channel: Any, stage: str, key: Any, lane: str = "primary") -> None:
    """Mensagem persistente com confirmação do broker; só ``{estágio, chave}``, sem conteúdo."""
    channel.basic_publish(
        exchange="",
        routing_key=queue_name(stage, lane),
        body=json.dumps({"s": stage, "k": key}).encode(),
        mandatory=True,
        properties=pika.BasicProperties(
            delivery_mode=2, content_type="application/json", message_id=str(key)
        ),
    )


def decode(body: bytes) -> tuple[str, Any]:
    """Valida o corpo de uma mensagem; ``ValueError`` se não for ``{estágio, chave}``."""
    try:
        message = json.loads(body)
        stage, key = message["s"], message["k"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("Mensagem inválida") from None
    valid_key = (isinstance(key, str) and 0 < len(key) <= 128) or (
        isinstance(key, int) and not isinstance(key, bool) and key > 0
    )
    if stage not in STAGES or not valid_key:
        raise ValueError("Mensagem inválida")
    return stage, key


def depth(channel: Any, stage: str, lane: str = "primary") -> tuple[int, int]:
    """Mensagens esperando e consumidores ligados a uma fila (a base do painel de gargalos)."""
    method = channel.queue_declare(queue=queue_name(stage, lane), passive=True).method
    return int(method.message_count), int(method.consumer_count)


def delete_all(channel: Any) -> None:
    """Remove as filas do prefixo atual; usado por testes e pela limpeza de pilotos."""
    for stage in STAGES:
        for lane in LANES:
            channel.queue_delete(queue=queue_name(stage, lane))


def snapshot() -> list[dict[str, Any]]:
    """Profundidade e consumidores de cada fila, para o painel; fila inexistente vem sem números.

    Cada consulta usa o próprio canal: declarar passivamente uma fila que não existe fecha o canal.
    """
    connection = connect()
    try:
        rows = []
        for stage in STAGES:
            for lane in LANES:
                channel = connection.channel()
                try:
                    messages, consumers = depth(channel, stage, lane)
                    channel.close()
                except pika.exceptions.ChannelClosedByBroker:
                    messages = consumers = None  # type: ignore[assignment]
                rows.append(
                    dict(
                        stage=stage,
                        lane=lane,
                        queue=queue_name(stage, lane),
                        messages=messages,
                        consumers=consumers,
                    )
                )
        return rows
    finally:
        connection.close()
