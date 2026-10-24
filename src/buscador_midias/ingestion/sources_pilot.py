"""Teste finito de links conhecidos, separado das filas e do catálogo em produção."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from buscador_midias.catalog.migrate_postgres import create_tables
from buscador_midias.catalog.postgres import PostgresCatalogStore, connect_postgres
from buscador_midias.ingestion import broker
from buscador_midias.ingestion.artifacts import Artifacts
from buscador_midias.ingestion.engine import Engine
from buscador_midias.ingestion.g1_pilot import drain
from buscador_midias.ingestion.runtime import Service
from buscador_midias.ingestion.store import IngestStore

ROOT = Path(__file__).resolve().parents[3]


def setup(link_ids: list[int]) -> tuple[IngestStore, list[dict[str, Any]]]:
    """Copia cadastros selecionados sem alterar seus estados na base principal."""
    if not link_ids or len(link_ids) > 10 or len(set(link_ids)) != len(link_ids):
        raise ValueError("Escolha entre um e dez links distintos.")
    schema = "bm_sources_pilot_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S_")
    schema += uuid.uuid4().hex[:6]
    with connect_postgres() as db:
        db.row_factory = dict_row
        settings = db.execute(
            "SELECT config FROM midias_manager.ingest_settings WHERE id=1"
        ).fetchone()
        if not settings or any(settings["config"][k] for k in ("enabled", "workers_enabled")):
            raise ValueError("O fluxo geral precisa estar pausado para executar o piloto.")
        links = db.execute(
            "SELECT l.id,l.url,l.model,l.source_id,s.name FROM midias_manager.links l "
            "JOIN midias_manager.sites s ON s.id=l.source_id WHERE l.id=ANY(%s) "
            "AND l.active=1 AND s.active=1 AND l.deleted_at IS NULL AND s.deleted_at IS NULL "
            "ORDER BY l.source_id,l.id",
            (link_ids,),
        ).fetchall()
        if len(links) != len(link_ids):
            raise ValueError("Todos os links e sites selecionados devem estar ativos.")
        create_tables(db, schema)
        for table, condition, arguments in (
            ("mechanisms", "true", ()),
            ("sites", "id=ANY(%s)", ([r["source_id"] for r in links],)),
            ("links", "id=ANY(%s)", (link_ids,)),
        ):
            db.execute(
                sql.SQL("INSERT INTO {}.{} SELECT * FROM midias_manager.{} WHERE ").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)
                )
                + sql.SQL(condition),
                arguments,
            )
        db.execute(
            sql.SQL("INSERT INTO {}.catalog_imports(source_path,manifest) VALUES(%s,%s)").format(
                sql.Identifier(schema)
            ),
            ("pilot:midias_manager", Jsonb(dict(link_ids=link_ids))),
        )
    repo = IngestStore(PostgresCatalogStore(schema))
    repo.initialize()
    setting = repo.settings()
    repo.configure(
        dict(version=setting["version"], config=dict(max_attempts=1, window_mode="lookback"))
    )
    return repo, links


def cycle_summary(repo: IngestStore, run_id: str) -> dict[str, Any]:
    """Resume etapas e amostras sem apresentar URLs descobertas como textos coletados."""
    with repo.connection() as db:
        jobs = db.execute(
            "SELECT stage,state,outcome,count(*) n FROM ingest_jobs WHERE run_id=%s "
            "GROUP BY stage,state,outcome ORDER BY stage,state,outcome",
            (run_id,),
        ).fetchall()
        attempts = db.execute(
            "SELECT j.stage,a.outcome,a.detail,a.metrics FROM ingest_attempts a "
            "JOIN ingest_jobs j ON j.id=a.job_id WHERE j.run_id=%s ORDER BY a.started_at",
            (run_id,),
        ).fetchall()
        samples = db.execute(
            "SELECT a.url,j.state,j.outcome,a.status,a.payload->>'published_at' published_at,"
            "a.payload->>'title' title,length(a.payload->>'body') text_chars "
            "FROM ingest_jobs j JOIN ingest_articles a ON a.hash=j.hash "
            "WHERE j.run_id=%s AND j.stage='fetch' AND j.attempts>0",
            (run_id,),
        ).fetchall()
        discovered = db.execute(
            "SELECT count(*) n FROM ingest_occurrences WHERE run_id=%s",
            (run_id,),
        ).fetchone()["n"]
        new_jobs = db.execute(
            "SELECT count(*) n FROM ingest_jobs WHERE run_id=%s AND stage='dedup'",
            (run_id,),
        ).fetchone()["n"]
    return dict(
        run_id=run_id,
        discovered=discovered,
        new_dedup_jobs=new_jobs,
        jobs=jobs,
        attempts=attempts,
        samples=samples,
        discovery_complete=not any(
            r["stage"] == "discovery" and r["state"] != "succeeded" for r in jobs
        ),
    )


def run(link_ids: list[int], samples: int = 3) -> int:
    """Executa duas consultas HTTP reais por link e uma amostra textual, sem análise paga."""
    if not 0 <= samples <= 3:
        raise ValueError("Escolha de zero a três notícias por link e consulta.")
    repo, links = setup(link_ids)
    os.environ["BM_QUEUE_PREFIX"] = "midias.sources_pilot." + repo.store.schema
    folder = ROOT / "var/ingestion/piloto-10-fontes" / repo.store.schema
    folder.mkdir(parents=True, exist_ok=True)
    service = Service(repo, Engine(repo, artifacts=Artifacts(folder / "html")))
    until = datetime.now(UTC)
    since = until - timedelta(hours=48)
    report: dict[str, Any] = dict(
        schema=repo.store.schema,
        mode="live",
        since=since.isoformat(),
        until=until.isoformat(),
        sample_limit_per_cycle=samples,
        sources=[],
        scope="Um link cadastrado por fonte; descoberta, corpus e amostras textuais. "
        "Contexto, evidência e gravação no corpus não executados.",
    )
    path = folder / "resultado.json"

    def save() -> None:
        path.write_text(json.dumps(repo.jsonable(report), ensure_ascii=False, indent=2))

    connection = broker.connect()
    channel = connection.channel()
    broker.declare(channel)
    try:
        for link in links:
            item = dict(link, cycles=[])
            report["sources"].append(item)
            print(json.dumps(dict(starting=link["name"], url=link["url"])), flush=True)
            for number in (1, 2):
                started = time.monotonic()
                current = repo.create_run(
                    dict(
                        link_ids=[link["id"]],
                        since=since.isoformat(),
                        until=until.isoformat(),
                    )
                )
                assert current is not None
                error = None
                timings = {}
                for stage in ("discovery", "dedup", "fetch"):
                    if stage == "fetch":
                        with repo.connection() as db:
                            db.execute(
                                "UPDATE ingest_jobs SET state='skipped',"
                                "outcome='pilot_sample_not_selected',finished_at=now() "
                                "WHERE run_id=%s AND stage='fetch' AND state='pending' "
                                "AND id NOT IN (SELECT j.id FROM ingest_jobs j "
                                "JOIN ingest_articles a ON a.hash=j.hash WHERE j.run_id=%s "
                                "AND j.stage='fetch' AND j.state='pending' ORDER BY "
                                "a.payload->>'published_hint' DESC NULLS LAST,j.id LIMIT %s)",
                                (current["id"], current["id"], samples),
                            )
                    before = time.monotonic()
                    try:
                        drain(service, channel, stage, before + 120)
                    except TimeoutError as failure:
                        error = str(failure)
                        break
                    timings[stage + "_seconds"] = round(time.monotonic() - before, 3)
                repo.maintain()
                summary = cycle_summary(repo, current["id"])
                summary.update(
                    cycle=number,
                    seconds=round(time.monotonic() - started, 3),
                    timings=timings,
                    error=error,
                )
                item["cycles"].append(summary)
                save()
                print(
                    json.dumps(
                        dict(
                            source=link["name"],
                            cycle=number,
                            seconds=summary["seconds"],
                            urls=summary["discovered"],
                            new_dedup_jobs=summary["new_dedup_jobs"],
                            samples=summary["samples"],
                            complete=summary["discovery_complete"],
                        ),
                        ensure_ascii=False,
                        default=str,
                    ),
                    flush=True,
                )
                if error or not summary["discovery_complete"]:
                    # Falha de acesso terminada permite uma segunda tentativa finita;
                    # pendência em aberto não permite iniciar outra leitura do mesmo link.
                    with repo.connection() as db:
                        pending = db.execute(
                            "SELECT count(*) n FROM ingest_polls "
                            "WHERE run_id=%s AND NOT discovery_done",
                            (current["id"],),
                        ).fetchone()["n"]
                    if pending:
                        break
        report["result"] = "finished"
    except Exception as error:
        report.update(result="interrupted", error=type(error).__name__ + ": " + str(error)[:500])
        raise
    finally:
        save()
        if connection.is_open:
            connection.close()
        print(json.dumps(dict(report=str(path), result=report.get("result"))), flush=True)
    return 0
