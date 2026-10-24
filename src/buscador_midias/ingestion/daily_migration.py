"""Separa operações antigas sem apagar linhas, filas ou documentos do corpus."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from buscador_midias.catalog.postgres import WRITE_LOCK, connect_postgres
from buscador_midias.ingestion.schema import DDL


def archive_backlog(
    queue_prefix: str = "midias.ingest",
    *,
    schema: str = "midias_manager",
    output: Path | None = None,
) -> dict[str, Any]:
    """Move tabelas em transação única; exige agendamento e workers previamente pausados."""
    archive = "midias_ingest_archive_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    with connect_postgres() as db:
        db.row_factory = dict_row
        db.execute("SET LOCAL lock_timeout='10s'")
        db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
        db.execute(sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(sql.Identifier(schema)))
        settings = db.execute("SELECT * FROM ingest_settings WHERE id=1 FOR UPDATE").fetchone()
        if not settings or any(settings["config"][k] for k in ("enabled", "workers_enabled")):
            raise ValueError("Pause os workers e o agendamento antes de arquivar.")
        tables = db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s "
            "AND left(tablename,7)='ingest_' ORDER BY tablename",
            (schema,),
        ).fetchall()
        counts = {}
        for row in tables:
            name = row["tablename"]
            count = db.execute(
                sql.SQL("SELECT count(*) n FROM {}").format(sql.Identifier(name))
            ).fetchone()
            assert count is not None
            counts[name] = count["n"]
        if counts.get("ingest_archives"):
            raise ValueError("O backlog deste fluxo já foi separado; consulte o arquivo existente.")
        if not counts.get("ingest_runs"):
            raise ValueError("Não há rodadas anteriores para arquivar.")
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(archive)))
        for row in tables:
            db.execute(
                sql.SQL("ALTER TABLE {} SET SCHEMA {}").format(
                    sql.Identifier(schema, row["tablename"]),
                    sql.Identifier(archive),
                )
            )
        db.execute(DDL)
        config = dict(
            settings["config"],
            enabled=False,
            workers_enabled=False,
            window_mode="today",
            timezone="America/Sao_Paulo",
            daily_routes_only=True,
            allow_undated=False,
        )
        db.execute("INSERT INTO ingest_settings(id,config) VALUES(1,%s)", (Jsonb(config),))
        manifest = dict(
            schema_name=archive,
            queue_prefix=queue_prefix,
            table_counts=counts,
            archived_at=datetime.now(UTC).isoformat(),
        )
        db.execute(
            "INSERT INTO ingest_archives(schema_name,queue_prefix,manifest) VALUES(%s,%s,%s)",
            (archive, queue_prefix, Jsonb(manifest)),
        )
    root = output or Path(__file__).resolve().parents[3] / "var/ingestion/daily"
    root.mkdir(parents=True, exist_ok=True)
    (root / "archive-manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
