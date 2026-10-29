"""Serviço supervisionável: agendador, publicador e workers por etapa sobre ``WorkStore``.

Cada papel pode rodar em seu próprio processo (``--role`` e ``--stage``): um worker por etapa
consome a sua fila RabbitMQ (``basic_consume``, então os consumidores aparecem no broker), o
publicador reserva no banco e envia, e o agendador cria rodadas e faz a manutenção. Threads só
existem dentro de um consumidor, para a concorrência de leitura de rede.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from psycopg import InterfaceError, OperationalError

from buscador_midias.db.store import WorkStore
from buscador_midias.ingestion.engine import (
    Engine,
    HostBusyError,
    ProviderUnavailableError,
    RetryableError,
)
from buscador_midias.ingestion.flow import (
    DEDUP_BATCH,
    QUEUE_LIMITS,
    ProviderBreaker,
    publication_outside,
    recovery_workers,
)
from buscador_midias.pipeline import broker

_LOG = logging.getLogger(__name__)
NETWORK_STAGES = ("discovery", "fetch")
MAX_PUBLISH = 100


class Pipeline:
    """Liga o store, o motor e o broker; sobrevive a queda de banco ou de RabbitMQ."""

    def __init__(
        self,
        store: WorkStore,
        engine: Engine | None = None,
        *,
        lease_interval: float = 100.0,
    ) -> None:
        """Uma instância não interfere nos workers nem nas filas das outras instâncias."""
        self.store = store
        self.engine = engine or Engine(store, host_slots=self.host_slot_ceiling)  # type: ignore[arg-type]
        self.stop = threading.Event()
        self.instance = uuid.uuid4().hex[:12]
        self.threads: list[threading.Thread] = []
        self.breaker = ProviderBreaker()
        self.lease_interval = lease_interval
        self._config_lock = threading.Lock()
        self._config_until = 0.0
        self._config: dict[str, Any] = {}
        self._capacity_lock = {stage: threading.Lock() for stage in broker.STAGES}
        self._capacity: dict[str, tuple[float, tuple[bool, str]]] = {}

    # --- configuração e capacidade ---------------------------------------------------------

    def configuration(self) -> dict[str, Any]:
        """Cache de dois segundos: não abre uma conexão por mensagem."""
        with self._config_lock:
            if time.monotonic() >= self._config_until:
                self._config = self.store.settings()["config"]
                self._config_until = time.monotonic() + 2
            return self._config

    def host_slot_ceiling(self) -> int:
        """Maior número de vagas que a publicação dá a um host; as travas do motor seguem."""
        config = self.configuration()
        return max(
            int(config.get("host_concurrency", 1)), int(config.get("host_concurrency_max", 1))
        )

    def capacity(self, stage: str) -> tuple[bool, str]:
        """Decisão de capacidade por etapa, válida por meio segundo e compartilhada."""
        with self._capacity_lock[stage]:
            cached = self._capacity.get(stage)
            if cached is not None and time.monotonic() < cached[0]:
                return cached[1]
            result = self.store.capacity(stage)
            self._capacity[stage] = (time.monotonic() + 0.5, result)
            return result

    # --- publicar --------------------------------------------------------------------------

    def publish_once(self, channel: Any, stage: str, lane: str = "primary") -> int:
        """Envia ao broker o que está esperando, sem passar do espaço da fila."""
        config = self.configuration()
        ready, _ = broker.depth(channel, stage, lane)
        room = QUEUE_LIMITS[stage] - ready
        if lane == "recovery":
            room = min(room, int(config["workers"][stage]))
        room = min(MAX_PUBLISH, room)
        if room <= 0:
            return 0
        network = stage in NETWORK_STAGES
        keys = self.store.publish(
            stage,
            lane,
            room,
            per_host=int(config.get("host_concurrency", 1)) if network else None,
            per_host_max=int(config.get("host_concurrency_max", 1)) if network else None,
            send=lambda found: self.send(channel, stage, lane, found),
        )
        return len(keys)

    @staticmethod
    def send(channel: Any, stage: str, lane: str, keys: list[Any]) -> None:
        """Entrega as chaves reservadas ao broker, uma mensagem confirmada por chave."""
        for key in keys:
            broker.publish(channel, stage, key, lane)

    # --- processar -------------------------------------------------------------------------

    @contextmanager
    def keep_lease(self, job: dict[str, Any]) -> Iterator[None]:
        """Renova a reserva enquanto a etapa roda; uma etapa longa não perde a tarefa."""
        done = threading.Event()

        def renew() -> None:
            while not done.wait(self.lease_interval):
                try:
                    if not self.store.renew_lease(job):
                        return
                except Exception as error:
                    _LOG.warning("Renovação de reserva: %s", type(error).__name__)

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join(timeout=1)

    def process(self, stage: str, key: Any, owner: str, lane: str = "primary") -> bool:
        """Executa a etapa de uma tarefa e confirma o resultado; ``False`` se a mensagem é velha."""
        claimed = self.store.claim_many(stage, [key], owner, lane=lane)
        if not claimed:
            return False
        job = claimed[0]
        try:
            with self.keep_lease(job):
                result = self.engine.execute(job)
            self.store.finish(job, result)
        except HostBusyError as error:
            self.store.failure(
                job,
                str(error) or "Host ocupado; aguardando",
                busy=True,
                defer_seconds=error.retry_after_seconds,
            )
        except ProviderUnavailableError as error:
            self.breaker.failure(stage, str(error))
            self.store.failure(
                job,
                str(error),
                busy=True,
                metrics=error.metrics,
                defer_seconds=int(self.breaker.pause),
                recovery=True,
            )
        except RetryableError as error:
            self.store.failure(job, str(error), metrics=error.metrics, retryable=error.retryable)
        except (OperationalError, InterfaceError):
            self.store.failure(
                job, "PostgreSQL temporariamente indisponível", busy=True, defer_seconds=30
            )
        except Exception as error:
            # Corpo de API, URL com segredo e credenciais não entram na mensagem.
            _LOG.error("Etapa %s falhou: %s", stage, type(error).__name__)
            self.store.failure(job, "Falha interna: " + type(error).__name__)
        return True

    def process_many(self, keys: list[Any], owner: str, lane: str = "primary") -> int:
        """Compara um lote de notícias com o corpus e confirma tudo em uma transação."""
        jobs = self.store.claim_many("dedup", keys, owner, lane=lane)
        if not jobs:
            return 0
        started = time.perf_counter()
        try:
            outputs = self.engine.dedup_many(jobs)
            looked_up = time.perf_counter()
            self.store.finish_many(jobs, outputs)
            _LOG.info(
                "Lote dedup: %s notícias; consulta=%.3fs checkpoint=%.3fs",
                len(jobs),
                looked_up - started,
                time.perf_counter() - looked_up,
            )
        except Exception as error:
            _LOG.error("Lote dedup: %s", type(error).__name__)
            for job in jobs:
                self.store.failure(job, "Falha no lote: " + type(error).__name__)
        return len(jobs)

    # --- manutenção e filtro de janela -----------------------------------------------------

    def scheduler_tick(self) -> None:
        """Uma volta do agendador: manutenção e, se for a hora, a rodada do slot."""
        self.store.maintain()
        self.store.create_run({}, scheduled=True)

    def filter_window_once(self, cursor: str = "") -> tuple[str, int]:
        """Fecha somente exclusões comprovadas ou configuradas; preserva as demais tarefas."""
        previews = self.store.window_candidates(cursor)
        if not previews:
            return "", 0
        decisions = []
        for preview in previews:
            outside = publication_outside(preview) or self.engine.routes.outside(
                preview, preview["article"]["url"], "article"
            )
            if outside:
                decisions.append((preview["stage"], preview["id"], outside))
        closed = self.store.apply_decisions(decisions, self.instance + ":window_filter")
        return previews[-1]["id"], closed

    # --- laços de cada papel ---------------------------------------------------------------

    def scheduler(self) -> None:
        """Cria rodadas e conserta o que ficou para trás, a cada 10 segundos."""
        while not self.stop.is_set():
            try:
                self.scheduler_tick()
            except Exception as error:
                _LOG.error("Agendador: %s", type(error).__name__)
            self.stop.wait(10)

    def window_filter(self) -> None:
        """Drena arquivos antigos já enfileirados sem baixar páginas nem chamar provedores."""
        cursor = ""
        while not self.stop.is_set():
            try:
                if not self.configuration()["workers_enabled"]:
                    self.stop.wait(2)
                    continue
                cursor, _ = self.filter_window_once(cursor)
                self.stop.wait(0.1 if cursor else 60)
            except Exception as error:
                _LOG.error("Filtro de janela: %s", type(error).__name__)
                self.stop.wait(5)

    def publisher(self, stage: str, lane: str = "primary") -> None:
        """Publica sem parar; se o broker cair, retoma sem perder nada (o banco manda)."""
        while not self.stop.is_set():
            connection = None
            try:
                connection = broker.connect()
                channel = connection.channel()
                broker.declare(channel)
                while not self.stop.is_set():
                    published = self.publish_once(channel, stage, lane)
                    connection.process_data_events(time_limit=0)
                    if not published:
                        self.stop.wait(0.5)
            except Exception as error:
                _LOG.error("Publicador %s: %s", stage, type(error).__name__)
                self.stop.wait(5)
            finally:
                if connection and connection.is_open:
                    with suppress(Exception):
                        connection.close()

    def should_wait(self, stage: str, number: int, lane: str) -> str | None:
        """Motivo para este worker não consumir agora (disjuntor, acúmulo ou pausa)."""
        config = self.configuration()
        if (reason := self.breaker.paused(stage)) is not None:
            return reason
        ok, reason = self.capacity(stage)
        if not ok:
            return reason
        limit = int(config["workers"][stage])
        if lane == "recovery":
            limit = recovery_workers(limit)
        if not config["workers_enabled"] or number >= limit:
            return "pausado"
        return None

    def handle(self, stage: str, body: bytes, owner: str, lane: str) -> bool:
        """Processa uma mensagem; mensagem inválida é descartada com registro."""
        try:
            found_stage, key = broker.decode(body)
        except ValueError:
            _LOG.warning("Mensagem inválida descartada em %s", stage)
            return True
        if found_stage != stage:
            _LOG.warning("Mensagem de %s na fila de %s descartada", found_stage, stage)
            return True
        self.process(stage, key, owner, lane)
        return True

    def worker(self, stage: str, number: int, lane: str = "primary") -> None:
        """Consome a fila da etapa e só confirma depois que o banco guardou o resultado."""
        owner = f"{self.instance}:{stage}:{lane}:{number}"
        batch_size = DEDUP_BATCH if stage == "dedup" else 1
        while not self.stop.is_set():
            connection = None
            try:
                connection = broker.connect()
                channel = connection.channel()
                broker.declare(channel)
                channel.basic_qos(prefetch_count=batch_size)
                while not self.stop.is_set():
                    if self.should_wait(stage, number, lane):
                        connection.process_data_events(time_limit=1)
                        self.stop.wait(0.5)
                        continue
                    self.consume(channel, stage, owner, lane, batch_size)
            except Exception as error:
                _LOG.error("Worker %s: %s", stage, type(error).__name__)
                self.stop.wait(5)
            finally:
                if connection and connection.is_open:
                    with suppress(Exception):
                        connection.close()

    def consume(self, channel: Any, stage: str, owner: str, lane: str, batch_size: int) -> None:
        """Consome até a fila esvaziar ou até haver motivo para pausar."""
        pending: list[tuple[int, Any]] = []
        queue = broker.queue_name(stage, lane)
        number = int(owner.rsplit(":", 1)[1])
        for method, _, body in channel.consume(queue, inactivity_timeout=0.5):
            if method is not None:
                try:
                    _, key = broker.decode(body)
                except ValueError:
                    channel.basic_reject(method.delivery_tag, requeue=False)
                    continue
                if stage == "dedup":
                    pending.append((method.delivery_tag, key))
                    if len(pending) < batch_size:
                        continue
                else:
                    self.process(stage, key, owner, lane)
                    channel.basic_ack(method.delivery_tag)
            if pending:
                self.process_many([key for _, key in pending], owner, lane)
                channel.basic_ack(pending[-1][0], multiple=True)
                pending.clear()
            if self.stop.is_set() or self.should_wait(stage, number, lane):
                break
        channel.cancel()

    # --- serviço ---------------------------------------------------------------------------

    def serve(self, role: str = "all", stage: str | None = None) -> None:
        """Permite separar os processos no supervisor ou executá-los juntos."""
        self.store.start_pool(max_size=24)
        targets: list[tuple[Any, tuple[Any, ...]]] = []
        if role in ("all", "scheduler"):
            targets.append((self.scheduler, ()))
        if role in ("all", "window"):
            targets.append((self.window_filter, ()))
        if role in ("all", "publisher"):
            for selected in [stage] if stage else broker.STAGES:
                targets.append((self.publisher, (selected, "primary")))
                targets.append((self.publisher, (selected, "recovery")))
        if role in ("all", "worker"):
            config = self.store.settings()["config"]
            for selected in [stage] if stage else broker.STAGES:
                for number in range(recovery_workers(config["workers"][selected])):
                    targets.append((self.worker, (selected, number, "recovery")))
                for number in range(config["workers"][selected]):
                    targets.append((self.worker, (selected, number, "primary")))
        for target, args in targets:
            thread = threading.Thread(target=target, args=args, daemon=False)
            self.threads.append(thread)
            thread.start()
        failed = False
        while not self.stop.wait(1):
            if any(not thread.is_alive() for thread in self.threads):
                failed = True
                self.stop.set()
        for thread in self.threads:
            thread.join(timeout=100)
        self.store.close_pool()
        if failed:
            raise RuntimeError("Uma thread encerrou; o supervisor deve reiniciar o serviço")


def main(argv: list[str] | None = None) -> int:
    """CLI do serviço: ``serve`` com papel e etapa, ``init`` e ``queues``."""
    parser = argparse.ArgumentParser(description="Buscador contínuo por filas RabbitMQ")
    parser.add_argument("command", choices=("serve", "init", "queues"))
    parser.add_argument(
        "--role", choices=("all", "scheduler", "publisher", "worker", "window"), default="all"
    )
    parser.add_argument("--stage", choices=broker.STAGES)
    args = parser.parse_args(argv)
    store = WorkStore()
    if args.command == "init":
        store.install()
        return 0
    if args.command == "queues":
        connection = broker.connect()
        try:
            channel = connection.channel()
            for stage in broker.STAGES:
                for lane in broker.LANES:
                    waiting, consumers = broker.depth(channel, stage, lane)
                    print(f"{broker.queue_name(stage, lane)}\t{waiting}\t{consumers}")
        finally:
            connection.close()
        return 0
    service = Pipeline(store)
    signal.signal(signal.SIGTERM, lambda *_: service.stop.set())
    signal.signal(signal.SIGINT, lambda *_: service.stop.set())
    service.serve(args.role, args.stage)
    return 0
