"""Fila finita e histórico persistente das duas rotinas de saúde das fontes."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from buscador_midias.catalog.bright_reader import bright_data_available
from buscador_midias.catalog.health_policy import classify
from buscador_midias.catalog.health_restoration import automatic_event
from buscador_midias.catalog.health_rules import next_run
from buscador_midias.catalog.health_schema import DDL
from buscador_midias.catalog.postgres import WRITE_LOCK, PostgresCatalogStore, connect_postgres
from buscador_midias.catalog.store import CatalogError


class HealthStore:
    """Opera no mesmo esquema de sites, sem alterar estados manuais ou provas anteriores."""

    def __init__(self, store: PostgresCatalogStore) -> None:
        """Mantém repositório e inicialização idempotente por processo."""
        self.store = store
        self.ready = False
        self.lock = threading.RLock()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Abre transação nativa com registros nomeados e esquema fixado."""
        with connect_postgres(self.store.env) as db:
            db.execute(
                sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(
                    sql.Identifier(self.store.schema)
                )
            )
            db.row_factory = dict_row
            yield db

    def initialize(self) -> None:
        """Cria somente relações novas; a primeira execução fica para daqui a 24 horas."""
        with self.lock:
            if self.ready:
                return
            self.store.initialize()
            with self.connection() as db:
                db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
                for statement in DDL.split(";"):
                    if statement.strip():
                        db.execute(statement)
                for kind in ("active", "recovery"):
                    db.execute(
                        "INSERT INTO health_settings(kind,next_run_at) "
                        "VALUES(%s,now()+interval '24 hours') ON CONFLICT DO NOTHING",
                        (kind,),
                    )
            self.ready = True

    def settings(self) -> list[dict[str, Any]]:
        """Retorna rotina, frequência e próxima execução."""
        self.initialize()
        with self.connection() as db:
            return list(db.execute("SELECT * FROM health_settings ORDER BY kind"))

    def configure(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        """Valida revisão, agenda e opção de fallback sem expor credenciais."""
        self.initialize()
        required = {"enabled", "interval_minutes", "workers", "max_attempts", "version"}
        optional = {
            "bright_data",
            "max_bright_requests",
            "activate_recovered",
            "schedule_hours",
            "mode",
            "scope",
        }
        if (
            kind not in ("active", "recovery")
            or not required.issubset(data)
            or set(data) - required - optional
        ):
            raise CatalogError("Configuração de saúde inválida.")
        with self.connection() as db:
            before = db.execute(
                "SELECT * FROM health_settings WHERE kind=%s FOR UPDATE", (kind,)
            ).fetchone()
            values = {**before, **data}
            if any(
                type(values[k]) is not bool
                for k in ("enabled", "bright_data", "activate_recovered")
            ) or any(
                type(values[k]) is not int or not low <= values[k] <= high
                for k, low, high in (
                    ("interval_minutes", 1, 525600),
                    ("workers", 1, 32),
                    ("max_attempts", 1, 3),
                    ("version", 1, 2147483647),
                    ("max_bright_requests", 1, 48),
                )
            ):
                raise CatalogError(
                    "Configuração inválida: confira frequência, paralelismo e tentativas."
                )
            hours = values["schedule_hours"]
            if (
                not isinstance(hours, list)
                or len(set(hours)) != len(hours)
                or any(type(h) is not int or not 0 <= h <= 23 for h in hours)
                or values["mode"] not in ("full", "probe")
                or values["scope"] not in ("errors", "inactive")
                or (kind == "recovery" and values["mode"] == "probe")
                or (kind == "active" and values["scope"] != "errors")
            ):
                raise CatalogError("Configuração inválida: confira horários, modo e escopo.")
            hours = sorted(hours)
            if before["version"] != values["version"]:
                raise CatalogError("A rotina mudou. Atualize a tela antes de salvar.", 409)
            if values["bright_data"] and not bright_data_available():
                raise CatalogError("Credenciais Bright Data não configuradas no servidor.")
            changed = (
                before["interval_minutes"] != values["interval_minutes"]
                or before["enabled"] != values["enabled"]
                or sorted(before["schedule_hours"]) != hours
            )
            next_at = (
                next_run(datetime.now(UTC), hours, values["interval_minutes"])
                if changed
                else before["next_run_at"]
            )
            after = db.execute(
                "UPDATE health_settings SET enabled=%s,interval_minutes=%s,workers=%s,"
                "max_attempts=%s,bright_data=%s,max_bright_requests=%s,activate_recovered=%s,"
                "schedule_hours=%s,mode=%s,scope=%s,"
                "version=version+1,next_run_at=%s,updated_at=now() WHERE kind=%s RETURNING *",
                (
                    values["enabled"],
                    values["interval_minutes"],
                    values["workers"],
                    values["max_attempts"],
                    values["bright_data"],
                    values["max_bright_requests"],
                    values["activate_recovered"],
                    hours,
                    values["mode"],
                    values["scope"],
                    next_at,
                    kind,
                ),
            ).fetchone()
            db.execute(
                "INSERT INTO events(entity,action,happened_at,before_json,after_json) "
                "VALUES('health_setting','update',now(),%s,%s)",
                (Jsonb(self.jsonable(before)), Jsonb(self.jsonable(after))),
            )
            return dict(after)

    @staticmethod
    def jsonable(value: Any) -> Any:
        """Converte datas recursivamente para snapshots JSON portáveis ao powerDD."""
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: HealthStore.jsonable(v) for k, v in value.items()}
        if isinstance(value, list):
            return [HealthStore.jsonable(v) for v in value]
        return value

    def create(
        self, kind: str, trigger: str = "manual", *, options: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Congela todos os elegíveis; não expande a fila com novas descobertas."""
        self.initialize()
        if kind not in ("active", "recovery"):
            raise CatalogError("Rotina desconhecida.")
        options = options or {}
        if (
            set(options) - {"bright_data", "activate_recovered", "scope"}
            or any(type(value) is not bool for key, value in options.items() if key != "scope")
            or options.get("scope", "errors") not in ("errors", "inactive", "auto_excluded_access")
        ):
            raise CatalogError("Opções da rodada inválidas.")
        if options.get("scope") in ("inactive", "auto_excluded_access") and (
            kind != "recovery" or trigger != "manual"
        ):
            raise CatalogError("A varredura completa de inativos deve ser manual.")
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            setting = db.execute(
                "SELECT * FROM health_settings WHERE kind=%s FOR UPDATE", (kind,)
            ).fetchone()
            if trigger == "scheduled" and (
                not setting["enabled"] or setting["next_run_at"] > datetime.now(UTC)
            ):
                return None
            if db.execute(
                "SELECT 1 FROM health_runs WHERE kind=%s "
                "AND status IN ('queued','running','cancelling')",
                (kind,),
            ).fetchone():
                if trigger == "scheduled":
                    return None
                raise CatalogError("Já existe uma rodada aberta para esta rotina.", 409)
            configuration = {**setting, **options}
            if configuration["bright_data"] and not bright_data_available():
                raise CatalogError("Credenciais Bright Data não configuradas no servidor.")
            models = {r["model"]: r for r in db.execute("SELECT * FROM mechanisms")}
            restore = configuration.get("scope") == "auto_excluded_access"
            condition = (
                "l.active=1 AND s.active=1" if kind == "active" else "(l.active=0 OR s.active=0)"
            )
            rows = db.execute(
                "SELECT l.id,l.source_id,l.url,l.kind,l.model,l.version,l.last_test,"
                "s.version source_version,s.active source_active,s.name source_name,"
                "s.url source_url,h.category "
                "health_category,"
                "h.link_version health_link_version,h.source_version health_source_version,"
                "h.model_version health_model_version,h.checked_at health_checked_at,"
                "hr.snapshot health_snapshot FROM links l JOIN sites s ON "
                "s.id=l.source_id "
                "LEFT JOIN link_health h ON h.link_id=l.id "
                "LEFT JOIN health_results hr ON hr.run_id=h.run_id AND hr.link_id=l.id "
                + ("WHERE l.deleted_at IS NOT NULL " if restore else "WHERE l.deleted_at IS NULL ")
                + "AND s.deleted_at IS NULL AND "
                + condition
                + " ORDER BY l.id"
            ).fetchall()
            ident = uuid.uuid4().hex
            db.execute(
                "INSERT INTO health_runs(id,kind,trigger,status,configuration) "
                "VALUES(%s,%s,%s,'queued',%s)",
                (ident, kind, trigger, Jsonb(self.jsonable(configuration))),
            )
            if restore:
                eligible = []
                for row in rows:
                    if (
                        classify(row["last_test"] or {}) != "access_error"
                        or not automatic_event(
                            db, "endpoint", row["id"], "validation_exclude", row["version"]
                        )
                        or (
                            not row["source_active"]
                            and not automatic_event(
                                db,
                                "source",
                                row["source_id"],
                                "validation_inactivate",
                                row["source_version"],
                            )
                        )
                    ):
                        continue
                    eligible.append(row)
                rows = eligible
            total = 0
            with db.cursor().copy(
                "COPY health_results(run_id,link_id,source_id,host,snapshot) FROM STDIN"
            ) as copy:
                for row in rows:
                    model = models[row["model"]]
                    if kind == "recovery" and configuration.get("scope", "errors") == "errors":
                        health_snapshot = row["health_snapshot"] or {}
                        health_config = health_snapshot.get("config", {})
                        current = (
                            health_config.get("url") == row["url"]
                            and health_config.get("kind") == row["kind"]
                            and health_config.get("model") == row["model"]
                            and health_snapshot.get("source", {}).get("url") == row["source_url"]
                            and health_config.get("model_version") == model["version"]
                        )
                        previous = row["last_test"] or {}
                        manual_current = (
                            previous.get("url") == row["url"]
                            and previous.get("source_url") == row["source_url"]
                            and previous.get("model") == row["model"]
                            and previous.get("model_version") == model["version"]
                        )
                        if current and previous.get("observed_at"):
                            observed = datetime.fromisoformat(previous["observed_at"])
                            if observed > row["health_checked_at"]:
                                current = False
                        category = (
                            row["health_category"]
                            if current
                            else classify(previous)
                            if manual_current
                            else None
                        )
                        if category != "access_error":
                            continue
                    parameters = dict(
                        model["parameters"],
                        lookback_days=0,
                        article_samples=max(1, model["parameters"].get("article_samples", 3)),
                    )
                    if configuration["bright_data"]:
                        parameters["timeout_seconds"] = 60
                    snapshot = dict(
                        link_id=row["id"],
                        link_version=row["version"],
                        source_version=row["source_version"],
                        source=dict(
                            id=row["source_id"], name=row["source_name"], url=row["source_url"]
                        ),
                        config=dict(
                            bright_data=configuration["bright_data"],
                            access_transport=row.get("access_transport", "direct"),
                            max_bright_requests=configuration["max_bright_requests"],
                            url=row["url"],
                            kind=row["kind"],
                            model=row["model"],
                            model_version=model["version"],
                            parameters=parameters,
                        ),
                    )
                    if restore:
                        snapshot["restore_run_id"] = ident
                    copy.write_row(
                        (
                            ident,
                            row["id"],
                            row["source_id"],
                            urlsplit(row["source_url"]).hostname or "",
                            Jsonb(snapshot),
                        )
                    )
                    total += 1
            run = db.execute(
                "UPDATE health_runs SET total=%s WHERE id=%s RETURNING *", (total, ident)
            ).fetchone()
            if trigger == "scheduled":
                db.execute(
                    "UPDATE health_settings SET next_run_at=%s WHERE kind=%s",
                    (
                        next_run(
                            datetime.now(UTC),
                            setting["schedule_hours"],
                            setting["interval_minutes"],
                        ),
                        kind,
                    ),
                )
            return dict(run)

    def overview(self, offset: int = 0) -> dict[str, Any]:
        """Lista histórico paginado e contagens das decisões de cada rodada."""
        self.initialize()
        with self.connection() as db:
            runs = db.execute(
                "SELECT r.*,COALESCE(c.counts,'{}'::jsonb) counts,"
                "COALESCE(paid.n,0) bright_data_requests,COALESCE(activation.n,0) activated "
                "FROM health_runs r "
                "LEFT JOIN LATERAL (SELECT jsonb_object_agg(k,n) counts FROM (SELECT "
                "(CASE WHEN state='completed' THEN category ELSE state END) k,count(*) n FROM "
                "health_results WHERE run_id=r.id "
                "GROUP BY (CASE WHEN state='completed' THEN category ELSE state END)) x) c ON true "
                "LEFT JOIN LATERAL (SELECT "
                "sum(COALESCE((result->>'bright_data_requests')::int,0)) n "
                "FROM health_attempts WHERE run_id=r.id) paid ON true "
                "LEFT JOIN LATERAL (SELECT count(*) n FROM health_results "
                "WHERE run_id=r.id AND activated) activation ON true "
                "ORDER BY created_at DESC LIMIT 20 OFFSET %s",
                (offset,),
            ).fetchall()
            count = db.execute("SELECT count(*) n FROM health_runs").fetchone()["n"]
            worker = db.execute(
                "SELECT heartbeat_at FROM health_runs WHERE status='running' ORDER BY "
                "heartbeat_at DESC LIMIT 1"
            ).fetchone()
            return dict(
                settings=self.settings(), runs=runs, total=count, offset=offset, worker=worker
            )

    def details(self, ident: str, offset: int = 0, category: str = "") -> dict[str, Any]:
        """Resultados por link, filtros e todas as tentativas da página, sem expor HTML bruto."""
        self.initialize()
        with self.connection() as db:
            run = db.execute("SELECT * FROM health_runs WHERE id=%s", (ident,)).fetchone()
            if not run:
                raise CatalogError("Rodada não encontrada.", 404)
            where = "run_id=%s" + (
                " AND (CASE WHEN state='completed' THEN category ELSE state END)=%s"
                if category
                else ""
            )
            if category == "activated":
                where = "run_id=%s AND activated"
            args = (ident, category) if category and category != "activated" else (ident,)
            count = db.execute(
                "SELECT count(*) n FROM health_results WHERE " + where, args
            ).fetchone()["n"]
            items = db.execute(
                "SELECT * FROM health_results WHERE "
                + where
                + " ORDER BY link_id LIMIT 25 OFFSET %s",
                (*args, offset),
            ).fetchall()
            for item in items:
                item["history"] = db.execute(
                    "SELECT attempt,category,result,observed_at FROM health_attempts "
                    "WHERE run_id=%s AND link_id=%s ORDER BY attempt",
                    (ident, item["link_id"]),
                ).fetchall()
            return dict(run=run, items=items, total=count, offset=offset)

    def cancel(self, ident: str) -> dict[str, Any]:
        """Interrompe o restante da fila; testes em curso encerram dentro de seu orçamento."""
        with self.connection() as db:
            run = db.execute(
                "UPDATE health_runs SET status='cancelling' WHERE id=%s "
                "AND status IN ('queued','running') RETURNING *",
                (ident,),
            ).fetchone()
            if not run:
                raise CatalogError("Rodada já encerrada ou inexistente.", 409)
            return dict(run)
