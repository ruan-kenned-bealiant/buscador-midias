"""Agendador retomável: uma liderança PostgreSQL e filas finitas por rodada."""

from __future__ import annotations

import logging
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any

from buscador_midias.catalog.health_actions import apply_decision, change_transport
from buscador_midias.catalog.health_activation import activate_recovered
from buscador_midias.catalog.health_policy import classify
from buscador_midias.catalog.health_rules import decide, next_run
from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.link_review import ALTERNATE_TRANSPORTS, review_with_transports
from buscador_midias.catalog.postgres import WRITE_LOCK, connect_postgres
from buscador_midias.catalog.store import CatalogError
from buscador_midias.catalog.testing import ReaderFactory, run_test

_LOG = logging.getLogger(__name__)
LEADER_KEY = 674931206


class HealthWorker:
    """Executa enquanto o serviço do gerenciador está ligado, inclusive sem navegador aberto."""

    # Um site com muitos links na rodada (OCCRP tem milhares) aceita algumas leituras ao mesmo
    # tempo; lido um a um, ele sozinho prolongaria a rodada inteira por mais de uma hora.
    big_host_links = 100
    big_host_slots = 3

    def __init__(
        self,
        repository: HealthStore,
        factory: ReaderFactory | None = None,
        probe_reader: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        """Define portas de rede substituíveis (teste completo e sonda) para testar sem rede."""
        self.repository, self.factory, self.probe_reader = repository, factory, probe_reader
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.owner = uuid.uuid4().hex
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        """Inicia o agendador; não antecipa a primeira data configurada."""
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="catalog-health", daemon=True)
            self.thread.start()

    def close(self) -> None:
        """Para de distribuir trabalho e preserva os itens pendentes para retomada."""
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.repository.initialize()
                with connect_postgres(self.repository.store.env) as leader:
                    leader.autocommit = True
                    locked = leader.execute(
                        "SELECT pg_try_advisory_lock(hashtext(%s),%s)",
                        (self.repository.store.schema, LEADER_KEY),
                    ).fetchone()
                    if not locked or not locked[0]:
                        self.stop.wait(10)
                        continue
                    self.recover()
                    while not self.stop.is_set():
                        leader.execute("SELECT 1")
                        for kind in ("active", "recovery"):
                            self.repository.create(kind, "scheduled")
                        run = self.next_run()
                        if run:
                            self.execute_run(run, leader)
                        else:
                            self.wake.wait(10)
                            self.wake.clear()
            except Exception:
                _LOG.exception("Verificação de saúde interrompida; fila preservada para retomada")
                self.stop.wait(10)

    def recover(self) -> None:
        """Somente o novo líder retoma itens cujo processo anterior foi interrompido."""
        with self.repository.connection() as db:
            db.execute(
                "UPDATE health_runs SET owner=%s WHERE status IN ('running','cancelling')",
                (self.owner,),
            )
            db.execute(
                "UPDATE health_results q SET state='pending',detail='Retomado após interrupção',"
                "attempts=(SELECT count(*) FROM health_attempts a WHERE a.run_id=q.run_id AND "
                "a.link_id=q.link_id) "
                "WHERE q.state='running' AND EXISTS(SELECT 1 FROM health_runs r WHERE "
                "r.id=q.run_id "
                "AND r.status IN ('running','cancelling'))"
            )

    def next_run(self) -> dict[str, Any] | None:
        """Prioriza rodadas já iniciadas antes das novas."""
        with self.repository.connection() as db:
            run = db.execute(
                "SELECT * FROM health_runs WHERE status IN ('running','cancelling','queued') "
                "ORDER BY (status='queued'),created_at LIMIT 1 FOR UPDATE"
            ).fetchone()
            if not run:
                return None
            updated = db.execute(
                "UPDATE health_runs SET owner=%s,status=CASE WHEN status='queued' "
                "THEN 'running' ELSE status "
                "END,started_at=COALESCE(started_at,now()),heartbeat_at=now() "
                "WHERE id=%s RETURNING *",
                (self.owner, run["id"]),
            ).fetchone()
            return dict(updated) if updated else None

    def execute_run(self, run: dict[str, Any], leader: Any = None) -> None:
        """Um host por vez; nenhuma descoberta aumenta a fila congelada."""
        with self.repository.connection() as db:
            big_hosts = {
                r["host"]
                for r in db.execute(
                    "SELECT host FROM health_results WHERE run_id=%s GROUP BY host "
                    "HAVING count(*)>%s",
                    (run["id"], self.big_host_links),
                )
            }
        with ThreadPoolExecutor(
            max_workers=run["configuration"]["workers"], thread_name_prefix="health-link"
        ) as pool:
            tasks: dict[Any, str] = {}
            while not self.stop.is_set():
                if leader is not None:
                    leader.execute("SELECT 1")
                reserved: list[dict[str, Any]] = []
                with self.repository.connection() as db:
                    state = db.execute(
                        "UPDATE health_runs SET heartbeat_at=now() WHERE id=%s AND owner=%s "
                        "RETURNING status",
                        (run["id"], self.owner),
                    ).fetchone()
                    if not state:
                        return
                    if state["status"] == "cancelling":
                        db.execute(
                            "UPDATE health_results SET state='cancelled',finished_at=now() "
                            "WHERE run_id=%s AND state='pending'",
                            (run["id"],),
                        )
                    else:
                        for _ in range(run["configuration"]["workers"] - len(tasks)):
                            busy = Counter([*tasks.values(), *(r["host"] for r in reserved)])
                            full = [
                                host
                                for host, count in busy.items()
                                if count >= (self.big_host_slots if host in big_hosts else 1)
                            ]
                            row = db.execute(
                                "SELECT * FROM health_results WHERE run_id=%s AND state='pending' "
                                "AND host<>ALL(%s) ORDER BY link_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                                (run["id"], full),
                            ).fetchone()
                            if not row:
                                break
                            db.execute(
                                "UPDATE health_results SET state='running',started_at=now() "
                                "WHERE run_id=%s AND link_id=%s",
                                (run["id"], row["link_id"]),
                            )
                            # Submissão só depois do commit da reserva, abaixo.
                            reserved.append(row)
                for row in reserved:
                    tasks[pool.submit(self._test, run, row)] = row["host"]
                if not tasks:
                    with self.repository.connection() as db:
                        db.execute(
                            "UPDATE health_runs SET status=CASE WHEN status='cancelling' "
                            "THEN 'cancelled' ELSE 'completed' "
                            "END,finished_at=now(),heartbeat_at=now() "
                            "WHERE id=%s AND owner=%s",
                            (run["id"], self.owner),
                        )
                        setting = db.execute(
                            "SELECT schedule_hours,interval_minutes FROM health_settings "
                            "WHERE kind=%s",
                            (run["kind"],),
                        ).fetchone()
                        db.execute(
                            "UPDATE health_settings SET next_run_at=GREATEST(next_run_at,%s) "
                            "WHERE kind=%s",
                            (
                                next_run(
                                    datetime.now(UTC),
                                    setting["schedule_hours"],
                                    setting["interval_minutes"],
                                ),
                                run["kind"],
                            ),
                        )
                    return
                done, _ = wait(tasks, timeout=1, return_when=FIRST_COMPLETED)
                for task in done:
                    del tasks[task]
                    task.result()

    def _current(self, snapshot: dict[str, Any], kind: str) -> bool:
        if snapshot.get("restore_run_id"):
            from buscador_midias.catalog.health_restoration import restoration_current

            with self.repository.connection() as db:
                return restoration_current(db, snapshot)
        try:
            link = self.repository.store.endpoint(snapshot["link_id"])
            source = self.repository.store.source(snapshot["source"]["id"])
            model = self.repository.store.model_settings(snapshot["config"]["model"])
        except CatalogError:
            return False
        effective = link["active"] and source["active"]
        return bool(
            link["version"] == snapshot["link_version"]
            and source["version"] == snapshot["source_version"]
            and model["version"] == snapshot["config"]["model_version"]
            and (effective if kind == "active" else not effective)
        )

    def _review(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Sonda leve do link (só o documento) com as regras de frescor, período e tema."""
        config = snapshot["config"]
        link = dict(
            id=snapshot["link_id"],
            version=snapshot["link_version"],
            source_id=snapshot["source"]["id"],
            url=config["url"],
            label="",
            kind=config["kind"],
            model=config["model"],
            access_transport=config.get("access_transport", "direct"),
        )
        reader = self.probe_reader
        if reader is None:
            from buscador_midias.catalog.review_cli import _real_reader as reader
        return review_with_transports(link, reader, datetime.now(UTC))

    def _owned_and_running(self, run: dict[str, Any]) -> bool:
        with self.repository.connection() as db:
            state = db.execute(
                "SELECT status,owner FROM health_runs WHERE id=%s", (run["id"],)
            ).fetchone()
        return bool(state and state["owner"] == self.owner and state["status"] == "running")

    def _probe(self, run: dict[str, Any], row: dict[str, Any]) -> None:
        """Braço 1 leve: uma leitura do documento decide manter, inativar ou excluir."""
        snapshot = row["snapshot"]
        if self.stop.is_set():
            return
        if not self._current(snapshot, run["kind"]):
            self._finish(run, row, "skipped", None, "Cadastro alterado desde o início da rodada")
            return
        if not self._owned_and_running(run):
            self._finish(run, row, "cancelled", None, "Execução cancelada")
            return
        try:
            outcome = self._review(snapshot)
        except Exception:
            _LOG.exception("Erro interno na sonda do link %s", row["link_id"])
            self._finish(run, row, "error", None, "Falha interna; não é uma reprovação da fonte")
            return
        self._finish_probe(run, row, outcome)
        self.stop.wait(0.3)

    def _finish_probe(
        self, run: dict[str, Any], row: dict[str, Any], outcome: dict[str, Any]
    ) -> None:
        """Grava o resultado e aplica a decisão na mesma transação, na revisão sondada."""
        from psycopg.types.json import Jsonb

        snapshot = row["snapshot"]
        with self.repository.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            owned = db.execute(
                "SELECT owner FROM health_runs WHERE id=%s FOR UPDATE", (run["id"],)
            ).fetchone()
            if owned["owner"] != self.owner:
                return
            current = db.execute(
                "SELECT l.version,s.version source_version,m.version model_version "
                "FROM links l JOIN sites s ON s.id=l.source_id "
                "JOIN mechanisms m ON m.model=l.model "
                "WHERE l.id=%s AND l.deleted_at IS NULL AND s.deleted_at IS NULL",
                (row["link_id"],),
            ).fetchone()
            versions = (
                snapshot["link_version"],
                snapshot["source_version"],
                snapshot["config"]["model_version"],
            )
            if (
                not current
                or (
                    current["version"],
                    current["source_version"],
                    current["model_version"],
                )
                != versions
            ):
                db.execute(
                    "UPDATE health_results SET state='skipped',detail=%s,finished_at=now() "
                    "WHERE run_id=%s AND link_id=%s",
                    ("Cadastro alterado durante o teste", run["id"], row["link_id"]),
                )
                return
            previous = db.execute(
                "SELECT failures FROM link_health WHERE link_id=%s", (row["link_id"],)
            ).fetchone()
            decision = decide(outcome, previous["failures"] if previous else 0)
            link_version = apply_decision(db, snapshot, run["id"], decision) or versions[0]
            if outcome.get("transport_changed") and decision.action in ("keep", "review"):
                link_version = (
                    change_transport(
                        db,
                        dict(snapshot, link_version=link_version),
                        run["id"],
                        outcome["transport"],
                    )
                    or link_version
                )
            db.execute(
                "INSERT INTO health_attempts(run_id,link_id,attempt,category,result) "
                "VALUES(%s,%s,1,%s,%s)",
                (run["id"], row["link_id"], decision.category, Jsonb(outcome)),
            )
            db.execute(
                "UPDATE health_results SET state='completed',category=%s,result=%s,detail=%s,"
                "attempts=1,finished_at=now() WHERE run_id=%s AND link_id=%s",
                (decision.category, Jsonb(outcome), decision.reason, run["id"], row["link_id"]),
            )
            db.execute(
                "INSERT INTO link_health(link_id,run_id,category,link_version,source_version,"
                "model_version,failures) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(link_id) DO UPDATE SET run_id=excluded.run_id,"
                "category=excluded.category,checked_at=now(),link_version=excluded.link_version,"
                "source_version=excluded.source_version,model_version=excluded.model_version,"
                "failures=excluded.failures",
                (
                    row["link_id"],
                    run["id"],
                    decision.category,
                    link_version,
                    versions[1],
                    versions[2],
                    decision.failures,
                ),
            )

    def _test(self, run: dict[str, Any], row: dict[str, Any]) -> None:
        if run["configuration"].get("mode") == "probe" and run["kind"] == "active":
            self._probe(run, row)
            return
        snapshot = row["snapshot"]
        for attempt in range(row["attempts"] + 1, run["configuration"]["max_attempts"] + 1):
            if self.stop.is_set():
                return
            if not self._current(snapshot, run["kind"]):
                self._finish(
                    run, row, "skipped", None, "Cadastro alterado desde o início da rodada"
                )
                return
            with self.repository.connection() as db:
                state = db.execute(
                    "SELECT status,owner FROM health_runs WHERE id=%s", (run["id"],)
                ).fetchone()
                if state["owner"] != self.owner or state["status"] != "running":
                    self._finish(run, row, "cancelled", None, "Execução cancelada")
                    return
            config = dict(snapshot["config"], test_id=uuid.uuid4().hex)
            try:
                result = run_test(config, snapshot["source"], lambda step: None, self.factory)
                category = classify(result)
            except Exception:
                _LOG.exception("Erro interno no teste periódico do link %s", row["link_id"])
                self._finish(
                    run, row, "error", None, "Falha interna; não é uma reprovação da fonte"
                )
                return
            final = category != "access_error" or attempt == run["configuration"]["max_attempts"]
            if not self._current(snapshot, run["kind"]):
                self._finish(
                    run,
                    row,
                    "skipped",
                    (attempt, category, result),
                    "Cadastro alterado durante o teste",
                )
                return
            reactivating = run["kind"] == "recovery" and run["configuration"].get(
                "activate_recovered"
            )
            if final and reactivating and category == "access_error":
                result, category = self._other_transports(config, snapshot, result, category)
            gate = None
            if (
                final
                and category == "healthy"
                and run["kind"] == "recovery"
                and run["configuration"].get("activate_recovered")
            ):
                gate = self._gate(snapshot)
            self._finish(
                run,
                row,
                "completed" if final else "running",
                (attempt, category, result),
                "",
                gate=gate,
            )
            if final:
                self.stop.wait(0.3)
                return

    def _other_transports(
        self,
        config: dict[str, Any],
        snapshot: dict[str, Any],
        result: dict[str, Any],
        category: str,
    ) -> tuple[dict[str, Any], str]:
        """Refaz o teste completo nos outros transportes; vale o primeiro que sai saudável."""
        current = config.get("access_transport", "direct")
        for transport in ALTERNATE_TRANSPORTS:
            if transport == current or self.stop.is_set():
                continue
            retry = dict(config, access_transport=transport, test_id=uuid.uuid4().hex)
            try:
                found = run_test(retry, snapshot["source"], lambda step: None, self.factory)
            except Exception:
                _LOG.exception("Erro ao testar o link %s com %s", snapshot["link_id"], transport)
                continue
            if classify(found) == "healthy":
                return found, "healthy"
        return result, category

    def _gate(self, snapshot: dict[str, Any]) -> str | None:
        """Motivo para NÃO reativar (frescor, período, tema, esporte); ``None`` libera."""
        try:
            outcome = self._review(snapshot)
        except Exception:
            _LOG.exception("Erro ao conferir o frescor do link %s", snapshot["link_id"])
            return "Não reativado: falha ao conferir o frescor"
        if outcome["proposal"] == "keep":
            return None
        why = decide(outcome, 0).reason or outcome.get("reason", "")
        return f"Não reativado: {why}"

    def _finish(
        self,
        run: dict[str, Any],
        row: dict[str, Any],
        state: str,
        observation: tuple[int, str, dict[str, Any]] | None,
        detail: str,
        *,
        gate: str | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self.repository.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            owned = db.execute(
                "SELECT owner FROM health_runs WHERE id=%s FOR UPDATE", (run["id"],)
            ).fetchone()
            if owned["owner"] != self.owner:
                return
            if state == "completed":
                snapshot = row["snapshot"]
                current = db.execute(
                    "SELECT l.version,s.version source_version,m.version model_version "
                    "FROM links l JOIN sites s ON s.id=l.source_id "
                    "JOIN mechanisms m ON m.model=l.model WHERE l.id=%s "
                    "AND l.deleted_at IS NULL AND s.deleted_at IS NULL",
                    (row["link_id"],),
                ).fetchone()
                if snapshot.get("restore_run_id"):
                    from buscador_midias.catalog.health_restoration import restoration_current

                    unchanged = restoration_current(db, snapshot)
                else:
                    unchanged = bool(
                        current
                        and (
                            current["version"],
                            current["source_version"],
                            current["model_version"],
                        )
                        == (
                            snapshot["link_version"],
                            snapshot["source_version"],
                            snapshot["config"]["model_version"],
                        )
                    )
                if not unchanged:
                    state, detail = "skipped", "Cadastro alterado durante o teste"
            category = observation[1] if observation else None
            result = observation[2] if observation else None
            if observation:
                db.execute(
                    "INSERT INTO health_attempts(run_id,link_id,attempt,category,result) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (run["id"], row["link_id"], observation[0], category, Jsonb(result)),
                )
            db.execute(
                "UPDATE health_results SET state=%s,category=%s,result=%s,detail=%s,"
                "attempts=COALESCE(%s,attempts),finished_at=CASE WHEN %s='running' THEN NULL "
                "ELSE now() END "
                "WHERE run_id=%s AND link_id=%s",
                (
                    state,
                    category,
                    Jsonb(result) if result else None,
                    detail,
                    observation[0] if observation else None,
                    state,
                    run["id"],
                    row["link_id"],
                ),
            )
            if state == "completed" and category:
                snapshot = row["snapshot"]
                if (
                    category == "healthy"
                    and run["kind"] == "recovery"
                    and run["configuration"].get("activate_recovered")
                ):
                    activated = False
                    if gate:
                        db.execute(
                            "UPDATE health_results SET detail=%s WHERE run_id=%s AND link_id=%s",
                            (gate, run["id"], row["link_id"]),
                        )
                    else:
                        activated = activate_recovered(db, snapshot, result or {})
                    if activated:
                        db.execute(
                            "UPDATE health_results SET activated=true "
                            "WHERE run_id=%s AND link_id=%s",
                            (run["id"], row["link_id"]),
                        )
                db.execute(
                    "INSERT INTO "
                    "link_health(link_id,run_id,category,link_version,source_version,"
                    "model_version) "
                    "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(link_id) DO UPDATE SET "
                    "run_id=excluded.run_id,"
                    "category=excluded.category,checked_at=now(),link_version=excluded.link_version,"
                    "source_version=excluded.source_version,model_version=excluded.model_version",
                    (
                        row["link_id"],
                        run["id"],
                        category,
                        snapshot["link_version"],
                        snapshot["source_version"],
                        snapshot["config"]["model_version"],
                    ),
                )
