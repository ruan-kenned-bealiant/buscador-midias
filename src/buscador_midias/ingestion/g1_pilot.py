"""Piloto finito do plano diário do g1, isolado do backlog e das filas de produção."""

from __future__ import annotations

import gzip
import json
import os
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.migrate_postgres import create_tables
from buscador_midias.catalog.postgres import PostgresCatalogStore, connect_postgres
from buscador_midias.ingestion import broker
from buscador_midias.ingestion.engine import Engine
from buscador_midias.ingestion.runtime import Service
from buscador_midias.ingestion.store import IngestStore
from buscador_midias.net.fetcher import FetchResult

ROOT = Path(__file__).resolve().parents[3]
INDEX = "https://g1.globo.com/sitemap/g1/sitemap.xml"


def setup(mode: str) -> tuple[IngestStore, int, dict[str, Path]]:
    """Copia somente a fonte e o índice, preservando seus IDs e o catálogo principal."""
    schema = "bm_g1_pilot_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    mechanism = get_mechanism("g1_daily")
    with connect_postgres() as db:
        db.row_factory = dict_row
        settings = db.execute(
            "SELECT config FROM midias_manager.ingest_settings WHERE id=1"
        ).fetchone()
        if not settings:
            raise ValueError("Configuração do fluxo geral ausente.")
        if settings["config"]["enabled"] or settings["config"]["workers_enabled"]:
            raise ValueError("Pause o fluxo geral antes de executar o piloto isolado.")
        source = db.execute(
            "SELECT id FROM midias_manager.sites WHERE url=%s AND deleted_at IS NULL",
            ("https://g1.globo.com",),
        ).fetchone()
        if not source:
            raise ValueError("Cadastro principal do g1 não encontrado.")
        link = db.execute(
            "SELECT id FROM midias_manager.links WHERE source_id=%s AND url=%s ORDER BY id LIMIT 1",
            (source["id"], INDEX),
        ).fetchone()
        if not link:
            raise ValueError("Índice oficial do g1 ausente do histórico do catálogo.")
        db.execute(
            "INSERT INTO midias_manager.mechanisms(model,parameters,updated_at) "
            "VALUES(%s,%s,now()) ON CONFLICT DO NOTHING",
            (mechanism.id, Jsonb(mechanism.defaults())),
        )
        cache_rows = db.execute(
            "SELECT url,evidence->>'count_file' file FROM midias_manager.links "
            "WHERE url LIKE 'https://g1.globo.com/sitemap/g1/%%' "
            "AND evidence->>'count_file' IS NOT NULL"
        ).fetchall()
        create_tables(db, schema)
        for table, condition, parameters in (
            ("mechanisms", "true", ()),
            ("sites", "id=%s", (source["id"],)),
            ("links", "id=%s", (link["id"],)),
        ):
            db.execute(
                sql.SQL("INSERT INTO {}.{} SELECT * FROM midias_manager.{} WHERE ").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)
                )
                + sql.SQL(condition),
                parameters,
            )
        db.execute(
            sql.SQL("UPDATE {}.links SET active=1,deleted_at=NULL,model='g1_daily'").format(
                sql.Identifier(schema)
            )
        )
        db.execute(
            sql.SQL("INSERT INTO {}.catalog_imports(source_path,manifest) VALUES(%s,%s)").format(
                sql.Identifier(schema)
            ),
            (
                "pilot:midias_manager",
                Jsonb(dict(mode=mode, source_id=source["id"], link_id=link["id"])),
            ),
        )
    files = {}
    for row in cache_rows:
        candidate = (ROOT / row["file"]).resolve()
        if candidate.is_relative_to(ROOT / "var") and candidate.is_file():
            files[row["url"]] = candidate
    repo = IngestStore(PostgresCatalogStore(schema))
    repo.initialize()
    if mode == "archive":
        settings = repo.settings()
        repo.configure(dict(version=settings["version"], config=dict(window_mode="lookback")))
    return repo, int(link["id"]), files


class ArchiveReader:
    """Reproduz bytes preservados, sem simular acesso ao site ou suporte real a ETag."""

    def __init__(self, files: dict[str, Path], calls: list[dict[str, Any]]) -> None:
        """Registra leituras locais separadamente de chamadas HTTP."""
        self.files, self.calls = files, calls
        self.requests = 0
        self.conditional_headers: dict[str, dict[str, str]] = {}

    def get(self, url: str) -> FetchResult:
        """Arquivo ausente é erro explícito, sem completar com documento inventado."""
        self.calls.append(dict(url=url, transport="archive"))
        if url not in self.files:
            return FetchResult(url, None, b"", "XML ausente do arquivo histórico do piloto")
        with gzip.open(self.files[url], "rb") as stream:
            content = stream.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024:
            raise ValueError("Arquivo histórico acima de 16 MiB")
        return FetchResult(url, 200, content, None, url, "application/xml")


def drain(service: Service, channel: Any, stage: str, deadline: float) -> None:
    """Processa uma etapa do piloto sem consumir nenhuma mensagem das filas gerais."""
    while time.monotonic() < deadline:
        sent = service.publish_once(channel, stage)
        messages = []
        for _ in range(100 if stage == "dedup" else 1):
            method, _, body = channel.basic_get(queue=broker.queue_name(stage), auto_ack=False)
            if not method:
                break
            messages.append((method.delivery_tag, json.loads(body)["job_id"]))
        if not messages:
            if not sent:
                return
            continue
        if stage == "dedup":
            service.process_many([ident for _, ident in messages], "g1-pilot:dedup")
        else:
            for _, ident in messages:
                service.process(ident, "g1-pilot:" + stage)
        for tag, _ in messages:
            channel.basic_ack(tag)
    raise TimeoutError("Tempo limite da etapa " + stage + " do piloto; checkpoints preservados")


def run(mode: str, selected_day: str | None = None, samples: int = 3) -> int:
    """Mede duas consultas; arquivo histórico não é apresentado como teste de notícias atuais."""
    if mode not in ("live", "archive") or not 0 <= samples <= 10:
        raise ValueError("Modo ou quantidade de amostras inválida.")
    if mode == "archive" and not selected_day:
        raise ValueError("Informe --day no modo archive; ele não representa notícias de hoje.")
    timezone = ZoneInfo("America/Sao_Paulo")
    day = date.fromisoformat(selected_day) if selected_day else datetime.now(timezone).date()
    since = datetime.combine(day, day_time.min, timezone)
    until = min(
        datetime.combine(day + timedelta(days=1), day_time.min, timezone), datetime.now(UTC)
    )
    if since >= until:
        raise ValueError("Selecione uma janela já iniciada.")
    repo, link, files = setup(mode)
    os.environ["BM_QUEUE_PREFIX"] = "midias.g1_pilot." + repo.store.schema
    calls: list[dict[str, Any]] = []
    engine = Engine(
        repo, reader_factory=(lambda *_: ArchiveReader(files, calls)) if mode == "archive" else None
    )
    service = Service(repo, engine)
    report: dict[str, Any] = dict(
        mode=mode,
        schema=repo.store.schema,
        queue_prefix=os.environ["BM_QUEUE_PREFIX"],
        since=since.isoformat(),
        until=until.isoformat(),
        cycles=[],
    )
    folder = ROOT / "var/ingestion/g1-piloto" / repo.store.schema
    folder.mkdir(parents=True, exist_ok=True)
    connection = broker.connect()
    channel = connection.channel()
    broker.declare(channel)
    try:
        for cycle in (1, 2):
            started = time.monotonic()
            with repo.connection() as db:
                before = db.execute("SELECT count(*) n FROM ingest_articles").fetchone()["n"]
                before_jobs = db.execute(
                    "SELECT count(*) n FROM ingest_jobs WHERE stage='dedup'"
                ).fetchone()["n"]
            current = repo.create_run(
                dict(link_ids=[link], since=since.isoformat(), until=until.isoformat())
            )
            assert current is not None
            with repo.connection() as db:
                db.execute(
                    "UPDATE ingest_runs SET trigger=%s WHERE id=%s",
                    ("g1_pilot_" + mode, current["id"]),
                )
            drain(service, channel, "discovery", time.monotonic() + 180)
            discovery_done = time.monotonic()
            drain(service, channel, "dedup", time.monotonic() + 180)
            with repo.connection() as db:
                rows = db.execute(
                    "SELECT state,outcome,result FROM ingest_jobs "
                    "WHERE run_id=%s AND stage='discovery'",
                    (current["id"],),
                ).fetchall()
                errors = [r["outcome"] for r in rows if r["state"] in ("retry", "failed")]
                if errors:
                    raise RuntimeError("Descoberta incompleta: " + "; ".join(errors))
                total = db.execute("SELECT count(*) n FROM ingest_articles").fetchone()["n"]
                jobs = db.execute(
                    "SELECT count(*) n FROM ingest_jobs WHERE stage='dedup'"
                ).fetchone()["n"]
                report["cycles"].append(
                    dict(
                        run_id=current["id"],
                        discovery_seconds=round(discovery_done - started, 3),
                        discovery_and_dedup_seconds=round(time.monotonic() - started, 3),
                        unique_registered=total,
                        new_registered=total - before,
                        new_dedup_jobs=jobs - before_jobs,
                        documents=rows,
                    )
                )
            if mode == "live" and cycle == 1 and samples:
                # O piloto valida a coleta completa de URLs e uma amostra da parte custosa.
                with repo.connection() as db:
                    db.execute(
                        "UPDATE ingest_jobs SET state='skipped',"
                        "outcome='pilot_sample_not_selected',finished_at=now() "
                        "WHERE stage='fetch' AND state='pending' AND id NOT IN "
                        "(SELECT id FROM ingest_jobs WHERE stage='fetch' AND state='pending' "
                        "ORDER BY created_at,id LIMIT %s)",
                        (samples,),
                    )
                for stage in ("fetch", "context", "evidence", "persist"):
                    drain(service, channel, stage, time.monotonic() + 180)
            repo.maintain()
        report["result"] = "completed"
    except Exception as error:
        report.update(result="incomplete", error=str(error)[:1000], error_type=type(error).__name__)
    finally:
        if connection.is_open:
            connection.close()
        with repo.connection() as db:
            report["articles"] = db.execute(
                "SELECT status,count(*) n FROM ingest_articles GROUP BY status"
            ).fetchall()
            report["jobs"] = db.execute(
                "SELECT stage,state,count(*) n FROM ingest_jobs GROUP BY stage,state"
            ).fetchall()
            report["attempts"] = db.execute(
                "SELECT j.stage,a.outcome,a.detail,a.metrics FROM ingest_attempts a "
                "JOIN ingest_jobs j ON j.id=a.job_id ORDER BY a.started_at"
            ).fetchall()
        report["archive_reads"] = calls
        report["article_validation"] = (
            "not_run_archive_replay" if mode == "archive" else "sample_limit_" + str(samples)
        )
        report["scope"] = (
            "XMLs selecionados e hashes completos; não equivale a coletar todos os textos"
        )
        (folder / "resultado.json").write_text(
            json.dumps(repo.jsonable(report), ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                dict(
                    result=report["result"],
                    report=str(folder / "resultado.json"),
                    cycles=[
                        {k: v for k, v in c.items() if k != "documents"} for c in report["cycles"]
                    ],
                    error=report.get("error"),
                ),
                ensure_ascii=False,
            )
        )
    return 0 if report["result"] == "completed" else 1
