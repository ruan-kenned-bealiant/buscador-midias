"""Serviço supervisionável: agendador, outbox e workers de seis filas RabbitMQ."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
import uuid
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

from psycopg import InterfaceError, OperationalError

from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.ingestion import broker
from buscador_midias.ingestion.admission import (
    confirm_published,
    host_backlogs,
    host_limit,
    network_candidates,
    primary_hosts,
)
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
from buscador_midias.ingestion.schema import STAGES
from buscador_midias.ingestion.store import IngestStore

_LOG = logging.getLogger(__name__)


class Service:
    """O serviço continua vivo com navegador fechado e reabre conexões após falhas."""

    def __init__(self, repository: IngestStore, engine: Engine | None = None) -> None:
        """Uma instância não interfere nos workers nem nas filas das outras instâncias."""
        self.repository = repository
        self.engine = engine or Engine(repository, host_slots=self.host_slot_ceiling)
        self.stop = threading.Event()
        self.instance = uuid.uuid4().hex
        self.threads: list[threading.Thread] = []
        self.breaker = ProviderBreaker()
        self.cleanup_every = 30.0
        self.cleaned_at: dict[tuple[str | None, bool], float] = {}
        self.config_lock = threading.Lock()
        self.config_until = 0.0
        self.cached_config: dict[str, Any] = {}
        self.capacity_locks = {stage: threading.Lock() for stage in STAGES}
        self.capacity_cache: dict[str, tuple[float, tuple[bool, str]]] = {}

    def heartbeat(self, owner: str, role: str, status: str, detail: str = "") -> None:
        """Telemetria indisponível não encerra uma thread nem mascara o erro original."""
        try:
            self.repository.heartbeat(owner, role, status, detail)
        except Exception as error:
            _LOG.warning("Heartbeat %s indisponível: %s", role, type(error).__name__)

    def configuration(self) -> dict[str, Any]:
        """Compartilha cache de dois segundos, evitando abrir conexão por mensagem."""
        with self.config_lock:
            if time.monotonic() >= self.config_until:
                self.cached_config = self.repository.settings()["config"]
                self.config_until = time.monotonic() + 2
            return self.cached_config

    def capacity(self, stage: str) -> tuple[bool, str]:
        """Compartilha por etapa uma decisão de capacidade válida por meio segundo."""
        with self.capacity_locks[stage]:
            cached = self.capacity_cache.get(stage)
            if cached is not None and time.monotonic() < cached[0]:
                return cached[1]
            result = self.repository.capacity(stage)
            self.capacity_cache[stage] = (time.monotonic() + 0.5, result)
            return result

    def cleanup_due(self, stage: str | None, recovery: bool) -> bool:
        """A faxina de eventos obsoletos varre toda a outbox; basta repeti-la a cada 30 s.

        Eventos obsoletos nunca são publicados de qualquer forma (a seleção exige a mesma
        geração e a tarefa aberta), então adiar o descarte só posterga a limpeza do banco.
        Cada publicador, por etapa e faixa, tem o seu próprio relógio.
        """
        key, now = (stage, recovery), time.monotonic()
        if now - self.cleaned_at.get(key, float("-inf")) < self.cleanup_every:
            return False
        self.cleaned_at[key] = now
        return True

    def publish_once(
        self, channel: Any, stage: str | None = None, *, recovery: bool = False
    ) -> int:
        """Confirma publicação antes de marcar a outbox; reentrega é idempotente."""
        recovery_limit = (
            min(QUEUE_LIMITS[stage], self.configuration()["workers"][stage])
            if recovery and stage is not None
            else 1
        )
        allowance = recovery_limit if recovery else 100
        if stage is not None and channel is not None:
            ready = channel.queue_declare(
                queue=broker.queue_name(stage, recovery=recovery), passive=True
            )
            allowance = min(
                allowance,
                max(
                    0,
                    (recovery_limit if recovery else QUEUE_LIMITS[stage])
                    - ready.method.message_count,
                ),
            )
        if not allowance:
            return 0
        with self.repository.publication_connection() as db:
            network = stage in ("discovery", "fetch")
            config = self.configuration()
            per_host = int(config.get("host_concurrency", 1))
            cap = int(config.get("host_concurrency_max", 1))
            occupied: dict[str, int] = {}
            limits: dict[str, int] = {}
            if network and cap > per_host:
                limits = {
                    host: host_limit(backlog, per_host, cap)
                    for host, backlog in host_backlogs(db, str(stage)).items()
                }
            if network:
                admitted = db.execute(
                    "SELECT pg_try_advisory_xact_lock(674931221) acquired"
                ).fetchone()
                if not admitted["acquired"]:
                    return 0
                for r in db.execute(
                    "SELECT COALESCE(a.url,j.payload->>'url') url FROM ingest_jobs j "
                    "LEFT JOIN ingest_articles a ON a.hash=j.hash "
                    "WHERE j.stage IN ('discovery','fetch') AND j.state IN ('queued','running')"
                ).fetchall():
                    host = (urlsplit(r["url"] or "").hostname or "").rstrip(".")
                    occupied[host] = occupied.get(host, 0) + 1
                if recovery:
                    # Não reservar um host para retentativa se há trabalho novo pronto nele.
                    for host in primary_hosts(db):
                        occupied[host] = limits.get(host, per_host)
            if self.cleanup_due(stage, recovery):
                db.execute(
                    "WITH obsolete AS (SELECT o.id FROM ingest_outbox o JOIN ingest_jobs j "
                    "ON j.id=o.job_id WHERE o.published_at IS NULL AND o.discarded_at IS NULL "
                    "AND (%s::text IS NULL OR o.stage=%s) AND (o.dispatch<>j.dispatch OR "
                    "(o.kind='work' AND j.state IN ('succeeded','failed','skipped','cancelled'))) "
                    "LIMIT 1000 FOR UPDATE OF o SKIP LOCKED) UPDATE ingest_outbox o "
                    "SET discarded_at=now(),discard_reason='completed_or_superseded' "
                    "FROM obsolete x WHERE o.id=x.id",
                    (stage, stage),
                )
            if network:
                rows = network_candidates(
                    db,
                    str(stage),
                    recovery,
                    occupied,
                    allowance,
                    per_host=per_host,
                    host_limits=limits,
                )
            else:
                rows = db.execute(
                    "SELECT o.id,o.job_id,o.kind,o.stage,COALESCE(a.url,j.payload->>'url') url "
                    "FROM ingest_outbox o "
                    "JOIN ingest_jobs j ON j.id=o.job_id "
                    "LEFT JOIN ingest_articles a ON a.hash=j.hash "
                    "WHERE o.published_at IS NULL AND o.discarded_at IS NULL AND o.ready_at<=now() "
                    "AND o.dispatch=j.dispatch AND ((o.kind='work' "
                    "AND j.state IN ('pending','retry')) "
                    "OR (o.kind='failed' AND j.state='failed')) "
                    "AND (%s::text IS NULL OR o.stage=%s) "
                    "AND (o.kind='failed' AND NOT %s OR o.kind='work' AND "
                    "(j.failures>0 OR j.payload @> '{\"recovery\":true}')=%s) "
                    "ORDER BY o.ready_at,o.id LIMIT %s FOR UPDATE OF o SKIP LOCKED",
                    (stage, stage, recovery, recovery, 1000 if network else allowance),
                ).fetchall()
            published = 0
            confirmed = []
            for row in rows:
                if published >= allowance:
                    break
                host = (urlsplit(row["url"] or "").hostname or "").rstrip(".")
                if network and row["kind"] == "work":
                    if occupied.get(host, 0) >= limits.get(host, per_host):
                        continue
                    occupied[host] = occupied.get(host, 0) + 1
                broker.publish(
                    channel,
                    row["stage"],
                    row["job_id"],
                    failed=row["kind"] == "failed",
                    recovery=recovery,
                )
                published += 1
                confirmed.append(row["id"])
                if network and row["kind"] == "work":
                    db.execute(
                        "INSERT INTO ingest_host_admission(host,last_admitted) VALUES(%s,now()) "
                        "ON CONFLICT(host) DO UPDATE SET last_admitted=excluded.last_admitted",
                        (host,),
                    )
            if confirmed:
                # Só trava tarefas depois de receber todos os confirms do lote.
                confirm_published(db, confirmed)
            return published

    def host_slot_ceiling(self) -> int:
        """Maior número de vagas que a admissão pode dar a um host; as travas do motor seguem."""
        config = self.configuration()
        return max(
            int(config.get("host_concurrency", 1)), int(config.get("host_concurrency_max", 1))
        )

    def filter_window_once(self, cursor: str = "") -> tuple[str, int]:
        """Finaliza somente exclusões comprovadas/configuradas; preserva demais tarefas."""
        previews = self.repository.window_candidates(cursor)
        if not previews:
            return "", 0
        decisions = {}
        for preview in previews:
            outside = publication_outside(preview) or self.engine.routes.outside(
                preview, preview["article"]["url"], "article"
            )
            if outside:
                decisions[preview["id"]] = outside
        jobs = self.repository.claim_many(list(decisions), self.instance + ":window_filter")
        self.repository.finish_many(jobs, [decisions[j["id"]] for j in jobs])
        return previews[-1]["id"], len(jobs)

    def window_filter(self) -> None:
        """Drena arquivos antigos já enfileirados sem baixar páginas ou chamar provedores."""
        owner, cursor = self.instance + ":window_filter", ""
        while not self.stop.is_set():
            try:
                if not self.configuration()["workers_enabled"]:
                    self.stop.wait(2)
                    continue
                cursor, count = self.filter_window_once(cursor)
                self.heartbeat(owner, "window_filter", "online", f"{count} URLs filtradas")
                self.stop.wait(0.1 if cursor else 60)
            except Exception as error:
                _LOG.error("Filtro de janela: %s", type(error).__name__)
                self.heartbeat(owner, "window_filter", "error", type(error).__name__)
                self.stop.wait(5)

    def process_many(
        self, identifiers: list[str], owner: str, *, recovery: bool | None = None
    ) -> None:
        """Compara um lote no corpus e confirma checkpoints em uma transação."""
        jobs = self.repository.claim_many(identifiers, owner, recovery=recovery)
        if not jobs:
            return
        try:
            self.heartbeat(owner, "dedup", "processing", f"{len(jobs)} URLs")
            started = time.perf_counter()
            outputs = self.engine.dedup_many(jobs)
            looked_up = time.perf_counter()
            self.repository.finish_many(jobs, outputs)
            finished = time.perf_counter()
            _LOG.info(
                "Lote dedup: %s URLs; consulta=%.3fs checkpoint=%.3fs total=%.3fs",
                len(jobs),
                looked_up - started,
                finished - looked_up,
                finished - started,
            )
        except Exception as error:
            _LOG.error("Lote dedup: %s", type(error).__name__)
            for job in jobs:
                self.repository.failure(job, "Falha no lote: " + type(error).__name__)
        finally:
            self.heartbeat(owner, "dedup", "online")

    def process(
        self, ident: str, owner: str, channel: Any = None, *, recovery: bool | None = None
    ) -> bool:
        """Confirma mensagem apenas após resultado/transição persistidos no banco."""
        job = self.repository.claim(ident, owner, recovery=recovery)
        if not job:
            return False
        try:
            self.heartbeat(owner, job["stage"], "processing", ident)
            result = self.engine.execute(job)
            self.repository.finish(job, result)
        except HostBusyError as error:
            self.repository.failure(
                job,
                str(error) or "Host ocupado; aguardando",
                busy=True,
                defer_seconds=error.retry_after_seconds,
            )
        except ProviderUnavailableError as error:
            self.breaker.failure(job["stage"], str(error))
            self.repository.failure(
                job,
                str(error),
                busy=True,
                metrics=error.metrics,
                defer_seconds=int(self.breaker.pause),
                recovery=True,
            )
        except RetryableError as error:
            self.repository.failure(
                job, str(error), metrics=error.metrics, retryable=error.retryable
            )
        except (OperationalError, InterfaceError):
            self.repository.failure(
                job, "PostgreSQL temporariamente indisponível", busy=True, defer_seconds=30
            )
        except Exception as error:
            # Corpo de API, URL com segredo e credenciais não entram na mensagem.
            _LOG.error("Etapa %s falhou: %s", job["stage"], type(error).__name__)
            self.repository.failure(job, "Falha interna: " + type(error).__name__)
        finally:
            self.heartbeat(owner, job["stage"], "online")
        return True

    def scheduler(self) -> None:
        """Distribui apenas vencidos, com limite de lote que não descarta os demais links."""
        owner = self.instance + ":scheduler"
        while not self.stop.is_set():
            try:
                self.repository.maintain()
                self.repository.create_run({}, scheduled=True)
                self.heartbeat(owner, "scheduler", "online")
            except Exception as error:
                _LOG.error("Agendador: %s", type(error).__name__)
                self.heartbeat(owner, "scheduler", "error", type(error).__name__)
            self.stop.wait(10)

    def publisher(self, stage: str, recovery: bool = False) -> None:
        """Retoma eventos da outbox quando o broker volta, sem descartar trabalho."""
        owner = self.instance + ":publisher:" + stage + (":recovery" if recovery else "")
        while not self.stop.is_set():
            connection = None
            try:
                connection = broker.connect()
                channel = connection.channel()
                broker.declare(channel)
                heartbeat_at = 0.0
                while not self.stop.is_set():
                    published = self.publish_once(channel, stage, recovery=recovery)
                    if time.monotonic() >= heartbeat_at:
                        self.heartbeat(owner, "publisher", "online")
                        heartbeat_at = time.monotonic() + 10
                    connection.process_data_events(time_limit=0)
                    if not published:
                        self.stop.wait(0.5)
            except Exception as error:
                _LOG.error("Publicador: %s", type(error).__name__)
                self.heartbeat(
                    owner, "publisher", "error", "RabbitMQ indisponível: " + type(error).__name__
                )
                self.stop.wait(5)
            finally:
                if connection and connection.is_open:
                    with suppress(Exception):
                        connection.close()

    @staticmethod
    def take_message(
        channel: Any, stage: str, recovery: bool = False
    ) -> tuple[Any, Any, Any, bool]:
        """Workers principais ajudam a recuperação somente quando sua fila está vazia."""
        if not recovery:
            method, properties, body = channel.basic_get(
                queue=broker.queue_name(stage), auto_ack=False
            )
            if method:
                return method, properties, body, False
        method, properties, body = channel.basic_get(
            queue=broker.queue_name(stage, recovery=True), auto_ack=False
        )
        return method, properties, body, True

    def worker(self, stage: str, number: int, recovery: bool = False) -> None:
        """Consome sua fila independente, com pausa por configuração e heartbeat."""
        owner = f"{self.instance}:{stage}:{number}" + (":recovery" if recovery else "")
        while not self.stop.is_set():
            connection = None
            try:
                connection = broker.connect()
                channel = connection.channel()
                broker.declare(channel)
                heartbeat_at = 0.0
                while not self.stop.is_set():
                    config = self.configuration()
                    reason = self.breaker.paused(stage)
                    if reason is not None:
                        if time.monotonic() >= heartbeat_at:
                            self.heartbeat(owner, stage, "error", reason)
                            heartbeat_at = time.monotonic() + 10
                        connection.process_data_events(time_limit=1)
                        self.stop.wait(1)
                        continue
                    capacity, capacity_reason = self.capacity(stage)
                    if not capacity:
                        if time.monotonic() >= heartbeat_at:
                            self.heartbeat(owner, stage, "throttled", capacity_reason)
                            heartbeat_at = time.monotonic() + 10
                        connection.process_data_events(time_limit=0)
                        self.stop.wait(0.5)
                        continue
                    if not config["workers_enabled"] or number >= config["workers"][stage]:
                        if time.monotonic() >= heartbeat_at:
                            self.heartbeat(owner, stage, "paused")
                            heartbeat_at = time.monotonic() + 10
                        connection.process_data_events(time_limit=1)
                        self.stop.wait(1)
                        continue
                    method, _, body, selected_recovery = self.take_message(channel, stage, recovery)
                    if method:
                        try:
                            payload = json.loads(body)
                            ident = payload["job_id"]
                            if not isinstance(ident, str) or len(ident) != 32:
                                raise ValueError()
                        except (ValueError, KeyError, TypeError):
                            channel.basic_reject(method.delivery_tag, requeue=False)
                            continue
                        if stage == "dedup":
                            identifiers, tags = [ident], [method.delivery_tag]
                            for _ in range(DEDUP_BATCH - 1):
                                extra, _, content = channel.basic_get(
                                    queue=broker.queue_name(stage, recovery=selected_recovery),
                                    auto_ack=False,
                                )
                                if not extra:
                                    break
                                try:
                                    item = json.loads(content)["job_id"]
                                    if not isinstance(item, str) or len(item) != 32:
                                        raise ValueError()
                                except (ValueError, KeyError, TypeError):
                                    channel.basic_reject(extra.delivery_tag, requeue=False)
                                    continue
                                identifiers.append(item)
                                tags.append(extra.delivery_tag)
                            self.process_many(identifiers, owner, recovery=selected_recovery)
                            for tag in tags:
                                channel.basic_ack(tag)
                        else:
                            self.process(ident, owner, channel, recovery=selected_recovery)
                            channel.basic_ack(method.delivery_tag)
                    else:
                        if time.monotonic() >= heartbeat_at:
                            self.heartbeat(owner, stage, "online")
                            heartbeat_at = time.monotonic() + 10
                        connection.process_data_events(time_limit=0)
                        self.stop.wait(0.25)
            except Exception as error:
                _LOG.error("Worker %s: %s", stage, type(error).__name__)
                self.heartbeat(
                    owner, stage, "error", "RabbitMQ/worker indisponível: " + type(error).__name__
                )
                self.stop.wait(5)
            finally:
                if connection and connection.is_open:
                    with suppress(Exception):
                        connection.close()

    def serve(self, role: str = "all", stage: str | None = None) -> None:
        """Permite separar os processos no supervisor ou executá-los juntos no MVP."""
        self.repository.initialize()
        self.repository.start_pool()
        targets: list[tuple[Any, tuple[Any, ...]]] = []
        if role in ("all", "scheduler"):
            targets.append((self.scheduler, ()))
        if role == "all":
            targets.append((self.window_filter, ()))
        if role in ("all", "publisher"):
            for selected in STAGES:
                targets.append((self.publisher, (selected,)))
                targets.append((self.publisher, (selected, True)))
        if role in ("all", "worker"):
            config = self.repository.settings()["config"]
            for selected in [stage] if stage else STAGES:
                # Alterar concorrência exige reiniciar o serviço se ultrapassar os slots abertos.
                for number in range(recovery_workers(config["workers"][selected])):
                    targets.append((self.worker, (selected, number, True)))
                for number in range(config["workers"][selected]):
                    targets.append((self.worker, (selected, number)))
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
        self.repository.close_pool()
        if failed:
            raise RuntimeError("Uma thread encerrou; o supervisor deve reiniciar o serviço")


def main(argv: list[str] | None = None) -> int:
    """CLI do serviço e da inicialização aditiva das tabelas."""
    parser = argparse.ArgumentParser(description="Buscador contínuo por filas RabbitMQ")
    parser.add_argument(
        "command",
        choices=(
            "serve",
            "init",
            "status",
            "g1-pilot",
            "sources-pilot",
            "backfill-lastmod",
            "backfill-context-themes",
            "stop-runs",
            "ensure-indexes",
        ),
    )
    parser.add_argument(
        "--role", choices=("all", "scheduler", "publisher", "worker"), default="all"
    )
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--mode", choices=("live", "archive"), default="live")
    parser.add_argument("--day", help="Dia civil em Brasília (AAAA-MM-DD)")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--link-ids", type=int, nargs="+")
    parser.add_argument("--run-id", help="Rodada do backfill; padrão: a mais recente em execução")
    parser.add_argument("--hours", type=int, default=6, help="Idade máxima dos XMLs em cache")
    parser.add_argument(
        "--apply", action="store_true", help="Encerra as tarefas (padrão: relatório)"
    )
    args = parser.parse_args(argv)
    if args.command == "g1-pilot":
        from buscador_midias.ingestion.g1_pilot import run

        return run(args.mode, args.day, args.samples)
    if args.command == "sources-pilot":
        from buscador_midias.ingestion.sources_pilot import run as run_sources

        return run_sources(args.link_ids or [], args.samples)
    repository = IngestStore(PostgresCatalogStore())
    repository.initialize()
    if args.command == "init":
        print("Tabelas de ingestão prontas; agendamento inicialmente pausado.")
    elif args.command == "backfill-lastmod":
        from buscador_midias.ingestion.lastmod_backfill import backfill, load_hints

        with repository.connection() as db:
            target = db.execute(
                "SELECT id FROM ingest_runs WHERE (%s::text IS NULL AND state='running') "
                "OR id=%s ORDER BY created_at DESC LIMIT 1",
                (args.run_id, args.run_id),
            ).fetchone()
            hints = load_hints(db, args.hours) if target else {}
        if not target:
            print("Nenhuma rodada encontrada.")
            return 1
        report = backfill(repository, target["id"], hints, apply=args.apply)
        print(json.dumps(report, ensure_ascii=False, default=str))
    elif args.command == "backfill-context-themes":
        from buscador_midias.catalog.postgres import connect_postgres
        from buscador_midias.corpus.theme_backfill import backfill_themes

        with connect_postgres() as corpus:
            print(json.dumps(backfill_themes(corpus, apply=args.apply), ensure_ascii=False))
    elif args.command == "stop-runs":
        if not args.apply:
            print("Cancela o trabalho não analisado das rodadas abertas; confirme com --apply.")
            return 1
        print(json.dumps(repository.stop_runs(), ensure_ascii=False))
    elif args.command == "ensure-indexes":
        print(json.dumps(dict(created=repository.ensure_report_indexes()), ensure_ascii=False))
    elif args.command == "status":
        print(
            json.dumps(repository.jsonable(repository.overview()), ensure_ascii=False, default=str)
        )
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        service = Service(repository)
        for code in (signal.SIGTERM, signal.SIGINT):
            signal.signal(code, lambda *_: service.stop.set())
        service.serve(args.role, args.stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
