"""Rodada finita, retomável e auditada para conservar apenas links comprovadamente úteis."""

from __future__ import annotations

import csv
import heapq
import json
import shutil
import time
import uuid
import zlib
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from buscador_midias.catalog.batch_reader import BatchCache, BatchReader
from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.reader import TestLimitError, TestReader
from buscador_midias.catalog.schema import archive_test
from buscador_midias.catalog.store import CatalogError, CatalogStore, _stamp
from buscador_midias.catalog.testing import ReaderFactory, run_test

RETRY_SECONDS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_validation_runs (
 id TEXT PRIMARY KEY,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 window_until TEXT NOT NULL,lookback_days INTEGER NOT NULL,max_attempts INTEGER NOT NULL,
 configurations TEXT NOT NULL,detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS catalog_validation_links (
 run_id TEXT NOT NULL REFERENCES catalog_validation_runs(id),
 link_id INTEGER NOT NULL REFERENCES links(id),link_version INTEGER NOT NULL,
 source_version INTEGER NOT NULL,host TEXT NOT NULL,initial_model TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
 ready_at REAL NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,
 PRIMARY KEY(run_id,link_id)
);
CREATE INDEX IF NOT EXISTS validation_state ON catalog_validation_links(run_id,state);
CREATE TABLE IF NOT EXISTS catalog_validation_attempts (
 run_id TEXT NOT NULL,link_id INTEGER NOT NULL,attempt INTEGER NOT NULL,
 model TEXT NOT NULL REFERENCES mechanisms(model),outcome TEXT NOT NULL,
 usable INTEGER NOT NULL,result BLOB NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,link_id,attempt),
 FOREIGN KEY(run_id,link_id) REFERENCES catalog_validation_links(run_id,link_id)
);
CREATE TABLE IF NOT EXISTS link_cadence (
 link_id INTEGER PRIMARY KEY REFERENCES links(id),run_id TEXT NOT NULL
 REFERENCES catalog_validation_runs(id),status TEXT NOT NULL,confidence TEXT NOT NULL,
 samples INTEGER NOT NULL,interval_seconds REAL,observation TEXT NOT NULL,updated_at TEXT NOT NULL
);
"""


def initialize_batch(store: CatalogStore) -> None:
    """Cria as relações operacionais sem alterar as fontes ou as provas existentes."""
    with store.connection() as db:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)


def status(store: CatalogStore, run_id: str | None = None) -> dict[str, Any]:
    """Retorna progresso consultável pelo painel sem iniciar execução."""
    with store.connection() as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='catalog_validation_runs'"
        ).fetchone():
            return dict(available=False)
        run = db.execute(
            "SELECT * FROM catalog_validation_runs "
            + ("WHERE id=?" if run_id else "ORDER BY created_at DESC LIMIT 1"),
            (run_id,) if run_id else (),
        ).fetchone()
        if run is None:
            return dict(available=False)
        counts = dict(
            db.execute(
                "SELECT state,count(*) FROM catalog_validation_links WHERE run_id=? GROUP BY state",
                (run["id"],),
            )
        )
        attempts = db.execute(
            "SELECT count(*) FROM catalog_validation_attempts WHERE run_id=?", (run["id"],)
        ).fetchone()[0]
        return dict(
            available=True,
            id=run["id"],
            status=run["status"],
            detail=run["detail"],
            total=sum(counts.values()),
            counts=counts,
            attempts=attempts,
            tested=sum(
                counts.get(k, 0)
                for k in ("validated", "unconfirmed", "kept", "excluded", "conflict")
            ),
            lookback_days=run["lookback_days"],
            updated_at=run["updated_at"],
        )


def create_run(store: CatalogStore) -> str:
    """Congela todos os links não excluídos, inclusive inativos, e configura janela anual."""
    initialize_batch(store)
    with store.connection() as db:
        active = db.execute(
            "SELECT id FROM catalog_validation_runs WHERE status IN "
            "('collecting','paused','finalizing')"
        ).fetchone()
        if active:
            raise CatalogError("Já existe uma rodada retomável; use seu ID em --resume.", 409)
    for model in store.models():
        parameters = dict(lookback_days=365, article_samples=3, timeout_seconds=30, max_requests=48)
        if model["id"] in ("auto", "sitemap_tree"):
            parameters.update(max_depth=10, max_documents=24)
        store.save_model(model["id"], dict(version=model["version"], parameters=parameters))
    configurations = {m["id"]: store.model_settings(m["id"]) for m in store.models()}
    ident, now = uuid.uuid4().hex, _stamp()
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO catalog_validation_runs VALUES(?,?,?,?,?,?,?,?,?)",
            (ident, "collecting", now, now, now, 365, 3, json.dumps(configurations), ""),
        )
        rows = db.execute(
            "SELECT e.id,e.version,s.version,s.url,e.model FROM links e "
            "JOIN sites s ON s.id=e.source_id WHERE e.deleted_at IS NULL "
            "AND s.deleted_at IS NULL ORDER BY e.id"
        ).fetchall()
        db.executemany(
            "INSERT INTO catalog_validation_links(run_id,link_id,link_version,"
            "source_version,host,initial_model,updated_at) VALUES(?,?,?,?,?,?,?)",
            [
                (
                    ident,
                    r[0],
                    r[1],
                    r[2],
                    (urlsplit(r[3]).hostname or "").removeprefix("www."),
                    r[4],
                    now,
                )
                for r in rows
            ],
        )
    return ident


def _decode(payload: bytes) -> dict[str, Any]:
    return json.loads(zlib.decompress(payload))  # type: ignore[no-any-return]


class ValidationBatch:
    """Um processo, até 32 hosts simultâneos, um link por host e três tentativas por link."""

    def __init__(
        self,
        store: CatalogStore,
        run_id: str,
        *,
        workers: int = 32,
        factory: ReaderFactory | None = None,
    ) -> None:
        """Usa fila persistente e porta de rede substituível para testes determinísticos."""
        self.store, self.run_id, self.workers, self.factory = store, run_id, workers, factory
        self.cache = BatchCache()
        initialize_batch(store)
        with store.connection() as db:
            row = db.execute(
                "SELECT * FROM catalog_validation_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise CatalogError("Rodada inexistente.", 404)
            self.run = dict(row)
        self.configurations = json.loads(self.run["configurations"])

    def _attempt(self, task: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        attempt = task["attempts"] + 1
        with self.store.connection() as db:
            link = dict(db.execute("SELECT * FROM links WHERE id=?", (task["link_id"],)).fetchone())
            source = dict(
                db.execute("SELECT * FROM sites WHERE id=?", (link["source_id"],)).fetchone()
            )
            if (
                link["version"] != task["link_version"]
                or source["version"] != task["source_version"]
                or link["deleted_at"]
                or source["deleted_at"]
            ):
                db.execute(
                    "UPDATE catalog_validation_links SET state='conflict',updated_at=? "
                    "WHERE run_id=? AND link_id=?",
                    (_stamp(), self.run_id, link["id"]),
                )
                return "conflict", task
            previous = db.execute(
                "SELECT result FROM catalog_validation_attempts WHERE run_id=? "
                "AND link_id=? ORDER BY attempt DESC LIMIT 1",
                (self.run_id, link["id"]),
            ).fetchone()
            model = task["initial_model"] if attempt == 1 else "auto"
            if attempt == 3 and previous:
                last = _decode(previous[0])
                if last.get("kind"):
                    model = (
                        "rss"
                        if last["kind"] == "feed"
                        else "sitemap_tree"
                        if last.get("root_index")
                        else "sitemap"
                    )
            db.execute(
                "UPDATE catalog_validation_links SET state='running',attempts=?,updated_at=? "
                "WHERE run_id=? AND link_id=?",
                (attempt, _stamp(), self.run_id, link["id"]),
            )
        setting = self.configurations[model]
        config = dict(
            test_id=uuid.uuid4().hex,
            url=link["url"],
            model=model,
            kind=link["kind"] if model == "auto" else "feed" if model == "rss" else "sitemap",
            model_version=setting["version"],
            parameters=setting["parameters"],
            window_until=_stamp(),
        )
        readers: list[BatchReader] = []

        def factory(roots: list[str], progress: Any) -> BatchReader:
            reader = BatchReader(roots, progress, self.cache, setting["parameters"], attempt)
            readers.append(reader)
            return reader

        result = run_test(config, source, lambda _: None, self.factory or factory)
        result["validation_run_id"] = self.run_id
        result["cached_responses"] = sum(reader.cache_hits for reader in readers)
        state = (
            "validated" if result["usable_for_news"] else "unconfirmed" if attempt >= 3 else "retry"
        )
        task = dict(
            task, attempts=attempt, ready_at=time.time() + RETRY_SECONDS if state == "retry" else 0
        )
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO catalog_validation_attempts VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    link["id"],
                    attempt,
                    model,
                    result["outcome"],
                    int(result["usable_for_news"]),
                    zlib.compress(json.dumps(result).encode()),
                    _stamp(),
                ),
            )
            db.execute(
                "UPDATE catalog_validation_links SET state=?,ready_at=?,updated_at=? "
                "WHERE run_id=? AND link_id=?",
                (state, task["ready_at"], _stamp(), self.run_id, link["id"]),
            )
        return state, task

    def health(self) -> None:
        """Suspende decisões se controles de rede independentes ficarem todos inacessíveis."""
        if self.factory is not None:
            return
        controls = [
            "https://canaltech.com.br/smap/sitemap-news.xml",
            "https://www.theguardian.com/uk/rss",
        ]
        for url in controls:
            try:
                reader = TestReader([url], lambda _: None, seconds=15, requests=8)
                response = reader.get(url)
                if response.ok:
                    parse_document(response.content, response.final_url or url)
                    return
            except (OSError, ValueError, CatalogError, TestLimitError):
                continue
        raise OSError(
            "Controles de rede indisponíveis; rodada pausada, sem excluir por falha geral."
        )

    def collect(self) -> None:
        """Percorre a fila uma vez, reencaminhando somente as tentativas finitas pendentes."""
        self.health()
        if self.run["status"] == "completed":
            return
        with self.store.connection() as db:
            db.execute(
                "UPDATE catalog_validation_links SET state='retry',attempts=max(0,attempts-1) "
                "WHERE run_id=? AND state='running'",
                (self.run_id,),
            )
            db.execute(
                "UPDATE catalog_validation_runs SET status='collecting',detail='',updated_at=? "
                "WHERE id=?",
                (_stamp(), self.run_id),
            )
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM catalog_validation_links "
                    "WHERE run_id=? AND state IN ('pending','retry') ORDER BY attempts,link_id",
                    (self.run_id,),
                )
            ]
        queues: defaultdict[str, deque[dict[str, Any]]] = defaultdict(deque)
        for row in rows:
            queues[row["host"]].append(row)
        sequence = 0
        heap: list[tuple[float, int, str]] = []
        for host, queue in queues.items():
            heapq.heappush(heap, (queue[0]["ready_at"], sequence, host))
            sequence += 1
        futures: dict[Any, str] = {}
        completed, last_progress = 0, time.monotonic()
        last_health = time.monotonic()
        with ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="validate-link"
        ) as pool:
            while heap or futures:
                if time.monotonic() - last_health > 300:
                    self.health()
                    last_health = time.monotonic()
                if shutil.disk_usage(self.store.path.parent).free < 150 * 1024 * 1024:
                    raise OSError("Espaço livre abaixo de 150 MiB; fila preservada para retomada.")
                while heap and len(futures) < self.workers and heap[0][0] <= time.time():
                    _, _, host = heapq.heappop(heap)
                    task = queues[host].popleft()
                    futures[pool.submit(self._attempt, task)] = host
                if not futures:
                    time.sleep(min(1, max(0.01, heap[0][0] - time.time())))
                    continue
                ready, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                for future in ready:
                    host = futures.pop(future)
                    state, task = future.result()
                    if state == "retry":
                        queues[host].append(task)
                    if queues[host]:
                        heapq.heappush(heap, (queues[host][0]["ready_at"], sequence, host))
                        sequence += 1
                    completed += 1
                if time.monotonic() - last_progress >= 15:
                    with self.store.connection() as db:
                        db.execute(
                            "UPDATE catalog_validation_runs SET updated_at=? WHERE id=?",
                            (_stamp(), self.run_id),
                        )
                    print(json.dumps(status(self.store, self.run_id)), flush=True)
                    last_progress = time.monotonic()
                if completed and completed % 100 == 0:
                    with self.store.connection() as db:
                        db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def finalize(self) -> None:
        """Aplica decisões só depois de testar a fila inteira; conflitos não apagam edições."""
        self.health()
        with self.store.connection() as db:
            if db.execute(
                "SELECT 1 FROM catalog_validation_links WHERE run_id=? "
                "AND state IN ('pending','retry','running')",
                (self.run_id,),
            ).fetchone():
                raise CatalogError("Ainda existem links sem decisão; nada será excluído.")
            db.execute(
                "UPDATE catalog_validation_runs SET status='finalizing',updated_at=? WHERE id=?",
                (_stamp(), self.run_id),
            )
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT link_id FROM catalog_validation_links WHERE "
                    "run_id=? AND state IN ('validated','unconfirmed')",
                    (self.run_id,),
                )
            ]
        for index, ident in enumerate(ids):
            if shutil.disk_usage(self.store.path.parent).free < 150 * 1024 * 1024:
                raise OSError("Espaço abaixo de 150 MiB durante aplicação; retomada preservada.")
            self._apply(ident)
            if index % 100 == 0:
                with self.store.connection() as db:
                    db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute(
                "SELECT * FROM sites s WHERE deleted_at IS NULL AND active=1 "
                "AND NOT EXISTS(SELECT 1 FROM links e WHERE e.source_id=s.id "
                "AND e.deleted_at IS NULL)"
            ).fetchall():
                before = dict(row)
                db.execute(
                    "UPDATE sites SET active=0,version=version+1,updated_at=? WHERE id=?",
                    (_stamp(), row["id"]),
                )
                after = dict(db.execute("SELECT * FROM sites WHERE id=?", (row["id"],)).fetchone())
                self.store._event(db, "source", row["id"], "validation_inactivate", before, after)
            conflicts = db.execute(
                "SELECT count(*) FROM catalog_validation_links WHERE run_id=? AND state='conflict'",
                (self.run_id,),
            ).fetchone()[0]
            db.execute(
                "UPDATE catalog_validation_runs SET status=?,updated_at=?,detail=? WHERE id=?",
                (
                    "completed_with_conflicts" if conflicts else "completed",
                    _stamp(),
                    f"{conflicts} edições concorrentes preservadas; precisam de nova validação."
                    if conflicts
                    else "",
                    self.run_id,
                ),
            )

    def _apply(self, ident: int) -> None:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = dict(
                db.execute(
                    "SELECT * FROM catalog_validation_links WHERE run_id=? AND link_id=?",
                    (self.run_id, ident),
                ).fetchone()
            )
            before = dict(db.execute("SELECT * FROM links WHERE id=?", (ident,)).fetchone())
            source = dict(
                db.execute("SELECT * FROM sites WHERE id=?", (before["source_id"],)).fetchone()
            )
            payload = db.execute(
                "SELECT result FROM catalog_validation_attempts WHERE run_id=? "
                "AND link_id=? ORDER BY usable DESC,attempt DESC LIMIT 1",
                (self.run_id, ident),
            ).fetchone()
            result = _decode(payload[0])
            version = db.execute(
                "SELECT version FROM mechanisms WHERE model=?", (result["model"],)
            ).fetchone()[0]
            if (
                before["version"] != task["link_version"]
                or source["version"] != task["source_version"]
                or version != result["model_version"]
                or before["deleted_at"]
                or source["deleted_at"]
            ):
                db.execute(
                    "UPDATE catalog_validation_links SET state='conflict' "
                    "WHERE run_id=? AND link_id=?",
                    (self.run_id, ident),
                )
                return
            kept, now = bool(result["usable_for_news"]), _stamp()
            evidence = (
                json.dumps(self.store._test_evidence(result))
                if result["xml_valid"] and result["compatible"]
                else before["evidence"]
            )
            db.execute(
                "UPDATE links SET model=?,kind=?,last_test=?,evidence=?,deleted_at=?,active=?,"
                "version=version+1,updated_at=? WHERE id=?",
                (
                    result["model"],
                    result["requested_kind"],
                    json.dumps(result),
                    evidence,
                    None if kept else now,
                    before["active"] if kept else 0,
                    now,
                    ident,
                ),
            )
            archive_test(db, ident, result)
            after = dict(db.execute("SELECT * FROM links WHERE id=?", (ident,)).fetchone())
            self.store._event(
                db,
                "endpoint",
                ident,
                "validation_keep" if kept else "validation_exclude",
                before,
                after,
            )
            cadence = result.get("cadence", {})
            db.execute(
                "INSERT OR REPLACE INTO link_cadence VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    self.run_id,
                    cadence.get("status", "unknown"),
                    cadence.get("confidence", "insufficient"),
                    cadence.get("samples", 0),
                    cadence.get("interval_seconds"),
                    json.dumps(cadence),
                    now,
                ),
            )
            db.execute(
                "UPDATE catalog_validation_links SET state=?,updated_at=? "
                "WHERE run_id=? AND link_id=?",
                ("kept" if kept else "excluded", now, self.run_id, ident),
            )

    def report(self, folder: Path) -> None:
        """Exporta decisões e periodicidade sem exportar corpos de notícias."""
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "progresso.json").write_text(
            json.dumps(status(self.store, self.run_id), indent=2)
        )
        with (
            self.store.connection() as db,
            (folder / "links.csv").open("w", encoding="utf-8-sig", newline="") as f,
        ):
            writer = csv.writer(f)
            writer.writerow(
                [
                    "site",
                    "url",
                    "decisao",
                    "tentativas",
                    "mecanismo",
                    "periodicidade_segundos",
                    "confianca",
                ]
            )
            for r in db.execute(
                "SELECT s.name,e.url,t.state,t.attempts,e.model,c.interval_seconds,c.confidence "
                "FROM catalog_validation_links t JOIN links e ON e.id=t.link_id "
                "JOIN sites s ON s.id=e.source_id LEFT JOIN link_cadence c ON c.link_id=e.id "
                "WHERE t.run_id=? ORDER BY s.name,e.url",
                (self.run_id,),
            ):
                writer.writerow(tuple(r))
