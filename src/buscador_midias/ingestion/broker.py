"""Transporte RabbitMQ durável; o PostgreSQL é a fonte dos checkpoints."""

from __future__ import annotations

import json
import os
from typing import Any

import pika

from buscador_midias.ingestion.schema import STAGES


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
            client_properties={"connection_name": "buscador-midias-ingest"},
        )
    )


def queue_name(stage: str, *, recovery: bool = False) -> str:
    """Namespace separado das diligências; testes podem usar um prefixo descartável."""
    if stage not in STAGES:
        raise ValueError("Etapa desconhecida")
    return (
        os.environ.get("BM_QUEUE_PREFIX", "midias.ingest")
        + "."
        + stage
        + (".recovery" if recovery else "")
    )


def declare(channel: Any) -> None:
    """Cria filas de trabalho, recuperação e falhas nas seis etapas; isoladas do KyX."""
    for stage in STAGES:
        name = queue_name(stage)
        channel.queue_declare(queue=name + ".failed", durable=True)
        channel.queue_declare(
            queue=queue_name(stage, recovery=True),
            durable=True,
            arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": name + ".failed"},
        )
        channel.queue_declare(
            queue=name,
            durable=True,
            arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": name + ".failed"},
        )
    channel.basic_qos(prefetch_count=1)
    channel.confirm_delivery()


def publish(
    channel: Any, stage: str, ident: str, *, failed: bool = False, recovery: bool = False
) -> None:
    """Confirmação do broker mais mensagem persistente, sem texto ou HTML no corpo."""
    channel.basic_publish(
        exchange="",
        routing_key=queue_name(stage, recovery=recovery and not failed)
        + (".failed" if failed else ""),
        body=json.dumps({"job_id": ident}).encode(),
        mandatory=True,
        properties=pika.BasicProperties(
            delivery_mode=2, content_type="application/json", message_id=ident
        ),
    )
