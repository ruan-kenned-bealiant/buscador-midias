"""Agendador retomável das rodadas de saúde: uma liderança PostgreSQL e filas finitas por rodada."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any

from buscador_midias.catalog.health_policy import classify
from buscador_midias.catalog.health_rules import decide
from buscador_midias.catalog.link_review import (
    ALTERNATE_TRANSPORTS,
    real_reader,
    review_with_transports,
)
from buscador_midias.catalog.testing import ReaderFactory, run_test
from buscador_midias.db.health import KINDS, HealthRepository

_LOG = logging.getLogger(__name__)
LEADER_KEY = 674931206
INTERNAL_FAILURE = "Falha interna; não é uma reprovação da fonte"
CHANGED = "Cadastro alterado desde o início da rodada"


class HealthWorker:
    """Conduz as rodadas enquanto o serviço está ligado; só o líder trabalha."""

    # Um site com muitos links na rodada (OCCRP tem milhares) aceita algumas leituras ao mesmo
    # tempo; lido um a um, ele sozinho prolongaria a rodada inteira por mais de uma hora.
    big_host_links = 100
    big_host_slots = 3

    def __init__(
        self,
        repository: HealthRepository,
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
            self.thread = threading.Thread(target=self._loop, name="bm-health", daemon=True)
            self.thread.start()

    def close(self) -> None:
        """Para de distribuir trabalho e preserva os itens pendentes para retomada."""
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)

    def serve(self) -> None:
        """Bloqueia no laço até ``stop`` (papel ``health`` do serviço)."""
        self._loop()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                with self.repository.direct_connection() as leader:
                    leader.autocommit = True
                    locked = leader.execute(
                        "SELECT pg_try_advisory_lock(hashtext(%s),%s)",
                        (self.repository.schema, LEADER_KEY),
                    ).fetchone()
                    if not locked or not locked["pg_try_advisory_lock"]:
                        self.stop.wait(10)
                        continue
                    self.recover()
                    while not self.stop.is_set():
                        leader.execute("SELECT 1")
                        for kind in KINDS:
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
        self.repository.recover(self.owner)

    def next_run(self) -> dict[str, Any] | None:
        """Prioriza rodadas já iniciadas antes das novas."""
        return self.repository.next_run(self.owner)

    def execute_run(self, run: dict[str, Any], leader: Any = None) -> None:
        """Um host por vez (vários nos sites grandes); nenhuma descoberta aumenta a fila."""
        big_hosts = self.repository.big_hosts(run["id"], self.big_host_links)
        workers = run["config"]["workers"]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="health-link") as pool:
            tasks: dict[Any, str] = {}
            while not self.stop.is_set():
                if leader is not None:
                    leader.execute("SELECT 1")
                status, reserved = self.repository.reserve(
                    run,
                    self.owner,
                    list(tasks.values()),
                    workers - len(tasks),
                    big_hosts,
                    self.big_host_slots,
                )
                if status is None:
                    return
                for row in reserved:
                    tasks[pool.submit(self._test, run, row)] = row["host"]
                if not tasks:
                    self.repository.complete_run(run, self.owner)
                    return
                done, _ = wait(tasks, timeout=1, return_when=FIRST_COMPLETED)
                for task in done:
                    del tasks[task]
                    task.result()

    # --- um link ------------------------------------------------------------------------------

    def _owned_and_running(self, run: dict[str, Any]) -> bool:
        state = self.repository.run_state(run["id"])
        return bool(state and state[1] == self.owner and state[0] == "running")

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
        self.repository.finish(run, row, self.owner, state, observation, detail, gate=gate)

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
            reader = real_reader
        return review_with_transports(link, reader, datetime.now(UTC))

    def _probe(self, run: dict[str, Any], row: dict[str, Any]) -> None:
        """Braço leve: uma leitura do documento decide manter, inativar ou excluir."""
        snapshot = row["snapshot"]
        if self.stop.is_set():
            return
        if not self.repository.is_current(snapshot, run["kind"]):
            self._finish(run, row, "skipped", None, CHANGED)
            return
        if not self._owned_and_running(run):
            self._finish(run, row, "cancelled", None, "Execução cancelada")
            return
        try:
            outcome = self._review(snapshot)
        except Exception:
            _LOG.exception("Erro interno na sonda do link %s", row["link_id"])
            self._finish(run, row, "error", None, INTERNAL_FAILURE)
            return
        self.repository.finish_probe(run, row, self.owner, outcome)
        self.stop.wait(0.3)

    def _test(self, run: dict[str, Any], row: dict[str, Any]) -> None:
        config = run["config"]
        if config.get("mode") == "probe" and run["kind"] == "active":
            self._probe(run, row)
            return
        snapshot = row["snapshot"]
        reactivating = run["kind"] == "recovery" and config.get("activate_recovered")
        for attempt in range(row["attempts"] + 1, config["max_attempts"] + 1):
            if self.stop.is_set():
                return
            if not self.repository.is_current(snapshot, run["kind"]):
                self._finish(run, row, "skipped", None, CHANGED)
                return
            if not self._owned_and_running(run):
                self._finish(run, row, "cancelled", None, "Execução cancelada")
                return
            test_config = dict(snapshot["config"], test_id=uuid.uuid4().hex)
            try:
                result = run_test(test_config, snapshot["source"], lambda step: None, self.factory)
                category = classify(result)
            except Exception:
                _LOG.exception("Erro interno no teste periódico do link %s", row["link_id"])
                self._finish(run, row, "error", None, INTERNAL_FAILURE)
                return
            final = category != "access_error" or attempt == config["max_attempts"]
            if not self.repository.is_current(snapshot, run["kind"]):
                self._finish(
                    run,
                    row,
                    "skipped",
                    (attempt, category, result),
                    "Cadastro alterado durante o teste",
                )
                return
            if final and reactivating and category == "access_error":
                result, category = self._other_transports(test_config, snapshot, result, category)
            gate = None
            if final and category == "healthy" and reactivating:
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
