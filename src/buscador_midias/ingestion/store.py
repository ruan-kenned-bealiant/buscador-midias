"""Agenda e checkpoints transacionais: mensagens transportam somente identificadores."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import threading
import uuid
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.postgres import WRITE_LOCK
from buscador_midias.catalog.store import CatalogError
from buscador_midias.domain.url import url_hash
from buscador_midias.ingestion.flow import BACKLOG_LIMITS, RESERVE_BYTES, clock_slot, today_start
from buscador_midias.ingestion.schema import DDL, DEFAULTS, STAGES

TERMINAL = ("succeeded", "failed", "skipped", "cancelled")


class IngestStore:
    """Compartilha a conexão de Mídias e mantém todas as etapas auditáveis."""

    def __init__(self, store: Any) -> None:
        """Reutiliza a conexão do catálogo sem herdar a API de rotinas de saúde."""
        self.store = store
        self._base = HealthStore(store)
        self.ready = False
        self.lock = threading.RLock()

    def connection(self) -> AbstractContextManager[Any]:
        """Abre uma transação no esquema configurado do catálogo."""
        return self._base.connection()

    def capacity(self, stage: str) -> tuple[bool, str]:
        """Contém produtores antes de gerar mais tarefas; consumidores finais continuam."""
        downstream = dict(
            discovery="dedup",
            dedup="fetch",
            fetch="context",
            context="evidence",
            evidence="persist",
        ).get(stage)
        if (
            stage in ("discovery", "fetch")
            and shutil.disk_usage(Path(__file__).resolve().parents[3]).free < RESERVE_BYTES
        ):
            return False, "Reserva de disco: aguardando liberação de espaço"
        if downstream:
            limit = BACKLOG_LIMITS[downstream]
            with self.connection() as db:
                count = db.execute(
                    "SELECT count(*) n FROM (SELECT 1 FROM ingest_jobs "
                    "WHERE stage=%s AND state IN ('pending','queued','retry','running') "
                    "AND NOT (failures>0 OR payload @> '{\"recovery\":true}') "
                    "LIMIT %s) q",
                    (downstream, limit),
                ).fetchone()["n"]
            if count >= limit:
                return False, f"Aguardando escoamento da etapa {downstream} ({limit} tarefas)"
        return True, ""

    def save_plan(self, job: dict[str, Any], plan: dict[str, Any]) -> str:
        """Guarda uma expansão comprimida; cada continuação só produz um pequeno lote."""
        key = url_hash(job["payload"]["url"] + "#" + job["poll_id"])
        # url_hash remove fragmentos; o poll também compõe a identidade persistida.
        key = str(job["poll_id"]) + key
        body = gzip.compress(json.dumps(plan, ensure_ascii=False).encode(), compresslevel=4)
        with self.connection() as db:
            db.execute(
                "INSERT INTO ingest_document_plans(id,poll_id,body) VALUES(%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (key, job["poll_id"], body),
            )
        return key

    def read_plan(self, key: str) -> dict[str, Any]:
        """Recupera o documento já interpretado, sem repetir HTTP ou expansão recursiva."""
        with self.connection() as db:
            row = db.execute(
                "SELECT body FROM ingest_document_plans WHERE id=%s", (key,)
            ).fetchone()
        if not row:
            raise CatalogError("Plano de documento não encontrado.")
        return dict(json.loads(gzip.decompress(row["body"])))

    @staticmethod
    def document_identity(job: dict[str, Any]) -> tuple[int, str, str]:
        """Isola o cache por fonte e revisões, sem misturar mecanismos diferentes."""
        snapshot = job["snapshot"]
        revision = json.dumps(
            [
                snapshot["model"],
                snapshot["model_version"],
                snapshot["link_version"],
                snapshot["source"]["version"],
            ]
        )
        return int(snapshot["source"]["id"]), url_hash(job["payload"]["url"]), revision

    def cached_document(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """Retorna XML interpretado para revalidação condicional; não dispensa novo acesso."""
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM ingest_document_cache WHERE source_id=%s AND url_hash=%s "
                "AND revision=%s",
                self.document_identity(job),
            ).fetchone()
        if row:
            return dict(row, document=json.loads(gzip.decompress(row["document"])))
        return None

    def cache_document(self, job: dict[str, Any], document: Any, response: Any) -> None:
        """Preserva o XML completo interpretado; a seleção de dias é refeita a cada janela."""
        with self.connection() as db:
            db.execute(
                "INSERT INTO ingest_document_cache(source_id,url_hash,revision,url,"
                "content_hash,etag,last_modified,document) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(source_id,url_hash,revision) DO UPDATE SET "
                "content_hash=excluded.content_hash,etag=excluded.etag,"
                "last_modified=excluded.last_modified,document=excluded.document,checked_at=now()",
                (
                    *self.document_identity(job),
                    job["payload"]["url"],
                    hashlib.sha256(response.content).hexdigest(),
                    response.etag,
                    response.last_modified,
                    gzip.compress(json.dumps(document).encode(), compresslevel=4),
                ),
            )

    def window_candidates(self, cursor: str = "") -> list[dict[str, Any]]:
        """Pré-avalia tarefas já existentes sem esperar pela ordem antiga do RabbitMQ."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT j.id,j.stage,p.snapshot,a.url,a.payload,r.since,r.until "
                "FROM ingest_jobs j JOIN ingest_articles a ON a.hash=j.hash "
                "JOIN ingest_polls p ON p.id=j.poll_id JOIN ingest_runs r ON r.id=j.run_id "
                "WHERE j.id>%s AND j.stage IN ('dedup','fetch') AND "
                "j.state IN ('pending','queued','retry') AND j.available_at<=now() AND "
                "(a.url ~ '/20[0-9]{2}/[0-9]{2}/[0-9]{2}' "
                "OR a.payload->>'published_hint_basis'='xml_publication') "
                "ORDER BY j.id LIMIT 1000",
                (cursor,),
            ).fetchall()
        return [
            dict(
                id=r["id"],
                stage=r["stage"],
                snapshot=r["snapshot"],
                article=dict(url=r["url"], payload=r["payload"]),
                run=dict(since=r["since"], until=r["until"]),
            )
            for r in rows
        ]

    @staticmethod
    def jsonable(value: Any) -> Any:
        """Datas ISO e decimais exatos de custos, inclusive em agregações PostgreSQL."""
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {k: IngestStore.jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [IngestStore.jsonable(v) for v in value]
        return value

    def initialize(self) -> None:
        """Cria apenas tabelas novas, idempotentemente e com trava de migração."""
        if self.ready:
            return
        with self.lock, self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            db.execute(DDL)
            db.execute(
                "INSERT INTO ingest_settings(id,config) VALUES(1,%s) ON CONFLICT DO NOTHING",
                (Jsonb(DEFAULTS),),
            )
        self.ready = True

    def settings(self) -> dict[str, Any]:
        """Lê configurações atuais sem credenciais."""
        self.initialize()
        with self.connection() as db:
            return dict(db.execute("SELECT * FROM ingest_settings WHERE id=1").fetchone())

    def archives(self) -> list[dict[str, Any]]:
        """Lista os arquivos de operações preservadas, sem reativar seus jobs."""
        self.initialize()
        with self.connection() as db:
            return list(
                db.execute("SELECT * FROM ingest_archives ORDER BY archived_at DESC").fetchall()
            )

    def archive_history(self) -> dict[str, Any]:
        """Permite consultar as rodadas anteriores pelo painel, com acesso somente de leitura."""
        from psycopg import sql

        archives = self.archives()
        result = []
        with self.connection() as db:
            for archive in archives:
                runs = db.execute(
                    sql.SQL(
                        "SELECT id,trigger,state,since,until,created_at,finished_at "
                        "FROM {}.ingest_runs ORDER BY created_at DESC LIMIT 500"
                    ).format(sql.Identifier(archive["schema_name"]))
                ).fetchall()
                result.append(dict(archive, runs=runs))
        return dict(archives=result)

    def configure(self, data: dict[str, Any]) -> dict[str, Any]:
        """Valida revisão, tipos e limites antes de alterar as próximas rodadas."""
        if set(data) - {"version", "config"} or type(data.get("version")) is not int:
            raise CatalogError("Configuração inválida.")
        patch = data.get("config")
        if not isinstance(patch, dict) or patch.keys() - DEFAULTS.keys():
            raise CatalogError("Parâmetros de busca desconhecidos.")
        if patch.get("bright_data") is True:
            raise CatalogError("O fallback Bright Data está desativado na busca de notícias.")
        for key, value in patch.items():
            if key in (
                "enabled",
                "workers_enabled",
                "daily_routes_only",
                "allow_undated",
                "bright_data",
                "evidence_paid_adverse_only",
            ):
                if type(value) is not bool:
                    raise CatalogError("Informe opções booleanas válidas.")
            elif key == "schedule_mode":
                if value not in ("clock", "link_interval"):
                    raise CatalogError("Selecione o modo de agendamento válido.")
            elif key == "schedule_hours":
                if (
                    not isinstance(value, list)
                    or not 1 <= len(value) <= 24
                    or any(type(hour) is not int or not 0 <= hour <= 23 for hour in value)
                    or len(set(value)) != len(value)
                ):
                    raise CatalogError("Informe horas distintas de 0 a 23.")
            elif key == "window_mode":
                if value not in ("today", "lookback"):
                    raise CatalogError("Selecione o modo de janela válido.")
            elif key == "timezone":
                if value not in ("America/Sao_Paulo", "UTC"):
                    raise CatalogError("Selecione um fuso horário válido.")
            elif key == "workers":
                if (
                    not isinstance(value, dict)
                    or set(value) != set(STAGES)
                    or any(type(v) is not int or not 1 <= v <= 16 for v in value.values())
                ):
                    raise CatalogError("Informe 1 a 16 workers por etapa.")
            elif key == "model":
                if not isinstance(value, str) or not re.fullmatch(
                    r"[A-Za-z0-9_./:-]{1,150}", value
                ):
                    raise CatalogError("Modelo inválido.")
            else:
                bounds = {
                    "lookback_minutes": (1, 5256000),
                    "max_attempts": (1, 3),
                    "retry_minutes": (1, 1440),
                    "analysis_text_chars": (1000, 30000),
                }
                low, high = bounds[key]
                if type(value) is not int or not low <= value <= high:
                    raise CatalogError(f"{key}: informe um inteiro entre {low} e {high}.")
        with self.connection() as db:
            old = db.execute("SELECT * FROM ingest_settings WHERE id=1 FOR UPDATE").fetchone()
            if old["version"] != data["version"]:
                raise CatalogError("A configuração mudou. Recarregue a tela.", 409)
            new = db.execute(
                "UPDATE ingest_settings SET config=%s,version=version+1,updated_at=now() "
                "WHERE id=1 RETURNING *",
                (Jsonb({**old["config"], **patch}),),
            ).fetchone()
            db.execute(
                "INSERT INTO events(entity,action,happened_at,before_json,after_json) "
                "VALUES('ingest_settings','update',now(),%s,%s)",
                (Jsonb(self.jsonable(old)), Jsonb(self.jsonable(new))),
            )
            return dict(new)

    @staticmethod
    def enqueue(
        db: Any,
        stage: str,
        subject: str,
        run_id: str,
        poll_id: str,
        payload: dict[str, Any],
        *,
        article_hash: str | None = None,
        generation: int = 1,
    ) -> str | None:
        """Cria tarefa e evento de outbox na mesma transação; evita reentregas duplicadas."""
        ident = uuid.uuid4().hex
        row = db.execute(
            "INSERT INTO ingest_jobs(id,stage,subject,run_id,poll_id,payload,hash,generation) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id",
            (ident, stage, subject, run_id, poll_id, Jsonb(payload), article_hash, generation),
        ).fetchone()
        if not row:
            return None
        db.execute(
            "INSERT INTO ingest_outbox(job_id,dispatch,stage) VALUES(%s,1,%s)", (ident, stage)
        )
        return ident

    def create_run(self, data: dict[str, Any], *, scheduled: bool = False) -> dict[str, Any] | None:
        """Congela links e janela; o agendamento só seleciona vencidos e nunca sobrepõe leituras."""
        self.initialize()
        if set(data) - {"link_ids", "since", "until"}:
            raise CatalogError("Opções da rodada desconhecidas.")
        ids = data.get("link_ids", [])
        if (
            not isinstance(ids, list)
            or len(ids) > 100000
            or any(type(i) is not int or i < 1 for i in ids)
        ):
            raise CatalogError("Lista de links inválida.")
        now = datetime.now(UTC)
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK + 10,))
            config = db.execute("SELECT config FROM ingest_settings WHERE id=1").fetchone()[
                "config"
            ]
            if scheduled and not config["enabled"]:
                return None
            fixed = scheduled and config.get("schedule_mode", "clock") == "clock"
            slot = None
            if fixed:
                slot = clock_slot(
                    now,
                    config.get("timezone", "America/Sao_Paulo"),
                    config.get("schedule_hours", [1, 6, 12, 16]),
                )
                if (
                    slot is None
                    or db.execute(
                        "SELECT 1 FROM ingest_clock_runs WHERE slot=%s", (slot,)
                    ).fetchone()
                ):
                    return None
            try:
                until = datetime.fromisoformat(data["until"]) if data.get("until") else now
                since = (
                    datetime.fromisoformat(data["since"])
                    if data.get("since")
                    else (
                        today_start(until, config.get("timezone", "America/Sao_Paulo"))
                        if config.get("window_mode", "today") == "today"
                        else until - timedelta(minutes=config["lookback_minutes"])
                    )
                )
                if config.get("window_mode", "today") == "today" and (
                    since < today_start(now, config.get("timezone", "America/Sao_Paulo"))
                    or until < today_start(now, config.get("timezone", "America/Sao_Paulo"))
                ):
                    raise ValueError()
                if (
                    since.tzinfo is None
                    or until.tzinfo is None
                    or since >= until
                    or until > now + timedelta(minutes=1)
                ):
                    raise ValueError()
            except (ValueError, TypeError):
                raise CatalogError(
                    "Informe datas válidas, com fuso e início anterior ao fim."
                ) from None
            db.execute(
                "INSERT INTO ingest_schedule(link_id) SELECT l.id FROM links l JOIN sites s "
                "ON s.id=l.source_id WHERE l.active=1 AND s.active=1 AND l.deleted_at IS NULL "
                "AND s.deleted_at IS NULL ON CONFLICT DO NOTHING"
            )
            rows = db.execute(
                "SELECT l.*,s.name source_name,s.url source_url,s.version source_version,"
                "s.country,s.language,m.version model_version,m.parameters "
                "FROM links l JOIN sites s ON s.id=l.source_id JOIN mechanisms m "
                "ON m.model=l.model JOIN ingest_schedule a ON a.link_id=l.id "
                "WHERE l.active=1 AND s.active=1 AND l.deleted_at IS NULL AND s.deleted_at IS NULL "
                "AND NOT EXISTS(SELECT 1 FROM ingest_polls p WHERE p.link_id=l.id AND "
                "NOT p.discovery_done) "
                "AND (%s=false OR a.next_due<=now()) AND (%s OR l.id=ANY(%s)) "
                "AND (%s=false OR l.model IN ('rss','news_sitemap','g1_daily') "
                "OR (l.kind='feed' AND l.model='auto') OR EXISTS "
                "(SELECT 1 FROM ingest_daily_routes d WHERE d.link_id=l.id "
                "AND d.purpose='updates')) "
                "AND (%s=false OR NOT EXISTS(SELECT 1 FROM ingest_daily_routes d "
                "WHERE d.link_id=l.id AND d.purpose IN ('archive','review'))) "
                "ORDER BY a.next_due,l.id "
                + ("LIMIT 500 " if scheduled and not fixed else "")
                + "FOR UPDATE OF a SKIP LOCKED",
                (
                    scheduled and not fixed,
                    not ids,
                    ids,
                    config.get("daily_routes_only", True) and (scheduled or not ids),
                    config.get("daily_routes_only", True),
                ),
            ).fetchall()
            if not rows:
                if scheduled:
                    return None
                raise CatalogError(
                    "Nenhum link ativo disponível; os selecionados podem estar em leitura.", 409
                )
            ident = uuid.uuid4().hex
            run = db.execute(
                "INSERT INTO ingest_runs(id,trigger,since,until,config) "
                "VALUES(%s,%s,%s,%s,%s) RETURNING *",
                (ident, "scheduled" if scheduled else "manual", since, until, Jsonb(config)),
            ).fetchone()
            if fixed:
                db.execute(
                    "INSERT INTO ingest_clock_runs(slot,run_id) VALUES(%s,%s)", (slot, ident)
                )
            for link in rows:
                poll = uuid.uuid4().hex
                snapshot = dict(
                    link_id=link["id"],
                    link_version=link["version"],
                    model=link["model"],
                    model_version=link["model_version"],
                    parameters=link["parameters"],
                    url=link["url"],
                    kind=link["kind"],
                    source=dict(
                        id=link["source_id"],
                        name=link["source_name"],
                        url=link["source_url"],
                        version=link["source_version"],
                        country=link["country"],
                        language=link["language"],
                    ),
                )
                db.execute(
                    "INSERT INTO ingest_polls(id,run_id,link_id,snapshot) VALUES(%s,%s,%s,%s)",
                    (poll, ident, link["id"], Jsonb(snapshot)),
                )
                self.enqueue(
                    db,
                    "discovery",
                    poll + ":" + url_hash(link["url"]),
                    ident,
                    poll,
                    dict(url=link["url"], depth=0),
                )
                db.execute(
                    "UPDATE ingest_schedule SET "
                    "last_started=now(),next_due=now()+%s*interval '1 second' WHERE "
                    "link_id=%s",
                    (link["search_interval_seconds"], link["id"]),
                )
            return dict(run, links=len(rows))

    def claim(
        self, ident: str, owner: str, *, recovery: bool | None = None
    ) -> dict[str, Any] | None:
        """Reserva apenas a revisão disponível da tarefa; mensagens duplicadas são inócuas."""
        rows = self.claim_many([ident], owner, recovery=recovery)
        return rows[0] if rows else None

    def claim_many(
        self, identifiers: list[str], owner: str, *, recovery: bool | None = None
    ) -> list[dict[str, Any]]:
        """Reserva um lote e carrega seus metadados com consultas em conjunto."""
        if not identifiers:
            return []
        with self.connection() as db:
            rows = db.execute(
                "UPDATE ingest_jobs SET state='running',owner=%s,attempts=attempts+1,"
                "lease_until=now()+interval '5 minutes',started_at=now(),updated_at=now() "
                "WHERE id=ANY(%s) AND state IN ('pending','queued','retry') AND "
                "available_at<=now() AND (%s::boolean IS NULL OR "
                "(failures>0 OR payload @> '{\"recovery\":true}')=%s) RETURNING *",
                (owner, identifiers, recovery, recovery),
            ).fetchall()
            if not rows:
                return []
            db.execute(
                "INSERT INTO ingest_attempts(job_id,attempt,owner) "
                "SELECT id,attempts,owner FROM ingest_jobs WHERE id=ANY(%s)",
                ([r["id"] for r in rows],),
            )
            polls = {
                p["id"]: p
                for p in db.execute(
                    "SELECT id,snapshot FROM ingest_polls WHERE id=ANY(%s)",
                    (list({r["poll_id"] for r in rows}),),
                ).fetchall()
            }
            runs = {
                r["id"]: r
                for r in db.execute(
                    "SELECT * FROM ingest_runs WHERE id=ANY(%s)",
                    (list({r["run_id"] for r in rows}),),
                ).fetchall()
            }
            articles = {
                a["hash"]: a
                for a in db.execute(
                    "SELECT * FROM ingest_articles WHERE hash=ANY(%s)",
                    ([r["hash"] for r in rows if r["hash"]],),
                ).fetchall()
            }
            result = []
            for row in rows:
                run = dict(runs[row["run_id"]])
                if row["payload"].get("retry_config"):
                    run["config"] = row["payload"]["retry_config"]
                result.append(
                    dict(
                        row,
                        snapshot=polls[row["poll_id"]]["snapshot"],
                        run=run,
                        article=articles.get(row["hash"]),
                    )
                )
            return result

    def failure(
        self,
        job: dict[str, Any],
        reason: str,
        *,
        busy: bool = False,
        metrics: dict[str, Any] | None = None,
        defer_seconds: int = 5,
        recovery: bool = False,
    ) -> str:
        """Espera crescente e limite de tentativas; contenção de host não reprova a fonte."""
        with self.connection() as db:
            current = db.execute(
                "SELECT * FROM ingest_jobs WHERE id=%s FOR UPDATE", (job["id"],)
            ).fetchone()
            if current["state"] != "running" or current["owner"] != job["owner"]:
                return "stale"
            exhausted = not busy and current["failures"] + 1 >= min(
                3, job["run"]["config"]["max_attempts"]
            )
            state = "failed" if exhausted else "retry"
            delay = (
                defer_seconds
                if busy
                else min(
                    3600,
                    60 * job["run"]["config"]["retry_minutes"] * 2 ** current["failures"],
                )
            )
            db.execute(
                "UPDATE ingest_attempts SET finished_at=now(),outcome=%s,detail=%s,metrics=%s "
                "WHERE job_id=%s AND attempt=%s",
                (
                    "deferred" if busy else state,
                    reason[:500],
                    Jsonb(metrics or {}),
                    job["id"],
                    job["attempts"],
                ),
            )
            db.execute(
                "UPDATE ingest_jobs SET state=%s,outcome=%s,result=%s,lease_until=NULL,"
                "available_at=now()+%s*interval '1 second',finished_at=CASE WHEN %s "
                "THEN now() ELSE NULL END,"
                "dispatch=dispatch+1,failures=failures+%s, "
                "payload=payload || %s,updated_at=now() WHERE id=%s",
                (
                    state,
                    reason[:500],
                    Jsonb(metrics or {}),
                    delay,
                    exhausted,
                    0 if busy else 1,
                    Jsonb({"recovery": True} if recovery else {}),
                    job["id"],
                ),
            )
            if exhausted:
                db.execute(
                    "INSERT INTO ingest_outbox(job_id,dispatch,kind,stage) "
                    "SELECT id,dispatch,'failed',stage FROM ingest_jobs WHERE id=%s",
                    (job["id"],),
                )
            if not exhausted:
                db.execute(
                    "INSERT INTO ingest_outbox(job_id,dispatch,ready_at,stage) "
                    "SELECT id,dispatch,available_at,stage FROM ingest_jobs WHERE id=%s",
                    (job["id"],),
                )
            if exhausted and job["hash"]:
                db.execute(
                    "UPDATE ingest_articles SET status='failed',updated_at=now() WHERE hash=%s",
                    (job["hash"],),
                )
            return state

    def finish(self, job: dict[str, Any], output: dict[str, Any], *, db: Any = None) -> bool:
        """Confirma resultado, documentos/URLs e próxima etapa atomicamente."""
        with self.connection() if db is None else nullcontext(db) as db:
            current = db.execute(
                "SELECT * FROM ingest_jobs WHERE id=%s FOR UPDATE", (job["id"],)
            ).fetchone()
            if current["state"] != "running" or current["owner"] != job["owner"]:
                return False
            for child in output.get("children", []):
                self.enqueue(
                    db,
                    "discovery",
                    job["poll_id"] + ":" + url_hash(child),
                    job["run_id"],
                    job["poll_id"],
                    dict(url=child, depth=job["payload"]["depth"] + 1),
                )
            continuation = output.get("continuation")
            if continuation:
                self.enqueue(
                    db,
                    "discovery",
                    job["poll_id"]
                    + ":"
                    + url_hash(job["payload"]["url"])
                    + ":slice:"
                    + str(continuation["cursor"]),
                    job["run_id"],
                    job["poll_id"],
                    dict(job["payload"], **continuation),
                )
            elif output.get("plan_key"):
                db.execute("DELETE FROM ingest_document_plans WHERE id=%s", (output["plan_key"],))
            candidates = output.get("candidates", [])
            for start in range(0, len(candidates), 500):
                batch = list(
                    {
                        url_hash(c["url"]): dict(
                            hash=url_hash(c["url"]),
                            url=c["url"],
                            payload=c,
                            job_id=uuid.uuid4().hex,
                        )
                        for c in candidates[start : start + 500]
                    }.values()
                )
                packed = Jsonb(batch)
                db.execute(
                    "WITH input AS (SELECT * FROM jsonb_to_recordset(%s) AS x(hash "
                    "text,url text,payload jsonb,job_id text)), "
                    "inserted AS (INSERT INTO "
                    "ingest_articles AS a(hash,url,payload,source_id,run_id,poll_id) "
                    "SELECT hash,url,payload,%s,%s,%s FROM input WHERE true ON CONFLICT(hash) DO "
                    "UPDATE SET generation=a.generation+1,status='pending',"
                    "run_id=excluded.run_id,poll_id=excluded.poll_id,"
                    "source_id=excluded.source_id,payload=excluded.payload,updated_at=now() "
                    "WHERE ((a.status='outside_window' AND "
                    "((a.payload->>'published_at')::timestamptz BETWEEN %s AND %s "
                    "OR ((a.payload->>'route_period_since')::timestamptz<=%s "
                    "AND (a.payload->>'route_period_until')::timestamptz>=%s))) "
                    "OR (a.status='date_unknown' AND %s)) "
                    "AND NOT EXISTS(SELECT 1 FROM ingest_jobs j WHERE j.hash=a.hash "
                    "AND j.state NOT IN ('succeeded','failed','skipped','cancelled')) "
                    "RETURNING hash,generation), "
                    "tasks AS (INSERT INTO ingest_jobs(id,stage,subject,hash,run_id,poll_id,"
                    "generation) "
                    "SELECT i.job_id,'dedup',i.hash,i.hash,%s,%s,n.generation FROM input i JOIN "
                    "inserted n ON n.hash=i.hash "
                    "ON CONFLICT DO NOTHING RETURNING id) INSERT INTO "
                    "ingest_outbox(job_id,dispatch,stage) SELECT id,1,'dedup' FROM tasks",
                    (
                        packed,
                        job["snapshot"]["source"]["id"],
                        job["run_id"],
                        job["poll_id"],
                        job["run"]["since"],
                        job["run"]["until"],
                        job["run"]["until"],
                        job["run"]["since"],
                        job["run"]["config"]["allow_undated"],
                        job["run_id"],
                        job["poll_id"],
                    ),
                )
                db.execute(
                    "INSERT INTO ingest_occurrences(hash,run_id,poll_id,document_url) "
                    "SELECT hash,%s,%s,%s FROM jsonb_to_recordset(%s) AS x(hash text) "
                    "ON CONFLICT DO NOTHING",
                    (job["run_id"], job["poll_id"], job["payload"]["url"], packed),
                )
            if job["hash"]:
                patch = output.get("article", {})
                db.execute(
                    "UPDATE ingest_articles SET payload=payload||%s,status=%s,"
                    "article_id=COALESCE(%s,article_id),updated_at=now() WHERE hash=%s",
                    (
                        Jsonb(patch),
                        output.get("status", output.get("next", "completed")),
                        output.get("article_id"),
                        job["hash"],
                    ),
                )
                if output.get("next"):
                    self.enqueue(
                        db,
                        output["next"],
                        job["hash"],
                        job["run_id"],
                        job["poll_id"],
                        {
                            k: v
                            for k, v in job["payload"].items()
                            if k in ("retry_config", "retry_config_version")
                        },
                        article_hash=job["hash"],
                        generation=job["generation"],
                    )
            summary = {
                k: v for k, v in output.items() if k not in ("candidates", "children", "article")
            }
            summary.update(
                candidates=len(output.get("candidates", [])),
                children=len(output.get("children", [])),
            )
            db.execute(
                "UPDATE ingest_jobs SET state='succeeded',outcome=%s,result=%s,finished_at=now(),"
                "lease_until=NULL,updated_at=now() WHERE id=%s",
                (output.get("outcome", "ok"), Jsonb(summary), job["id"]),
            )
            db.execute(
                "UPDATE ingest_attempts SET finished_at=now(),outcome='succeeded',metrics=%s "
                "WHERE job_id=%s AND attempt=%s",
                (Jsonb(summary.get("metrics", {})), job["id"], job["attempts"]),
            )
            return True

    def finish_many(self, jobs: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> None:
        """Um commit por lote, com pipeline SQL e checkpoints individuais preservados."""
        with self.connection() as db, db.pipeline():
            for job, output in zip(jobs, outputs, strict=True):
                self.finish(job, output, db=db)

    def maintain(self) -> None:
        """Recupera leases expirados, encerra leituras e fecha rodadas sem esperar para sempre."""
        with self.connection() as db:
            abandoned = db.execute(
                "SELECT j.*,r.config FROM ingest_jobs j JOIN ingest_runs r ON r.id=j.run_id "
                "WHERE j.state='running' AND j.lease_until<now() FOR UPDATE OF j SKIP LOCKED"
            ).fetchall()
            for row in abandoned:
                failed = row["failures"] + 1 >= min(3, row["config"]["max_attempts"])
                db.execute(
                    "UPDATE ingest_jobs SET "
                    "state=%s,failures=failures+1,owner=NULL,lease_until=NULL,"
                    "dispatch=dispatch+1,available_at=now(),outcome='Processo "
                    "interrompido',updated_at=now(),"
                    "finished_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s",
                    ("failed" if failed else "retry", failed, row["id"]),
                )
                db.execute(
                    "UPDATE ingest_attempts SET "
                    "finished_at=now(),outcome='interrupted',detail='Lease expirado; "
                    "retomada' "
                    "WHERE job_id=%s AND finished_at IS NULL",
                    (row["id"],),
                )
                db.execute(
                    "INSERT INTO ingest_outbox(job_id,dispatch,kind,stage) SELECT "
                    "id,dispatch,%s,stage FROM ingest_jobs WHERE id=%s",
                    ("failed" if failed else "work", row["id"]),
                )
                if failed and row["hash"]:
                    db.execute(
                        "UPDATE ingest_articles SET status='failed' WHERE hash=%s", (row["hash"],)
                    )
            polls = db.execute(
                "UPDATE ingest_polls p SET discovery_done=true WHERE NOT discovery_done "
                "AND NOT EXISTS(SELECT 1 FROM ingest_jobs j WHERE j.poll_id=p.id AND "
                "j.stage='discovery' "
                "AND j.state NOT IN ('succeeded','failed','skipped','cancelled')) RETURNING link_id"
            ).fetchall()
            for row in polls:
                db.execute(
                    "UPDATE ingest_schedule SET last_finished=now() WHERE link_id=%s",
                    (row["link_id"],),
                )
            db.execute(
                "DELETE FROM ingest_document_plans d USING ingest_polls p "
                "WHERE p.id=d.poll_id AND p.discovery_done"
            )
            # Inclui URLs compartilhadas em trânsito, mesmo quando processadas por outra rodada.
            db.execute(
                "UPDATE ingest_runs r SET state=CASE WHEN EXISTS(SELECT 1 FROM "
                "ingest_jobs j WHERE j.run_id=r.id AND (j.state='failed' OR "
                "COALESCE((j.result->>'coverage_partial')::boolean,false))) "
                "OR EXISTS(SELECT 1 FROM ingest_occurrences o JOIN ingest_articles a "
                "ON a.hash=o.hash WHERE o.run_id=r.id AND a.status IN "
                "('failed','date_unknown','evidence_pending')) "
                "THEN 'partial' ELSE 'completed' END,finished_at=now() WHERE r.state='running' "
                "AND NOT EXISTS(SELECT 1 FROM ingest_jobs j WHERE j.run_id=r.id AND "
                "j.state NOT IN ('succeeded','failed','skipped','cancelled')) "
                "AND NOT EXISTS(SELECT 1 FROM ingest_occurrences o JOIN ingest_jobs j "
                "ON j.hash=o.hash WHERE o.run_id=r.id "
                "AND j.state NOT IN ('succeeded','failed','skipped','cancelled'))"
            )

    def heartbeat(self, owner: str, role: str, status: str, detail: str = "") -> None:
        """Sinaliza serviço real e falhas sem expor credenciais."""
        with self.connection() as db:
            db.execute(
                "INSERT INTO ingest_workers(id,role,status,detail) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status,detail=excluded.detail,heartbeat_at=now()",
                (owner, role, status, detail[:300]),
            )

    def retry(self, ident: str) -> dict[str, Any]:
        """Reabre somente a etapa falhada e registra a ação; não repete etapas já concluídas."""
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK + 10,))
            job = db.execute(
                "SELECT * FROM ingest_jobs WHERE id=%s AND state='failed' FOR UPDATE", (ident,)
            ).fetchone()
            if not job:
                raise CatalogError("Tarefa não está na fila de falhas.", 409)
            if job["stage"] == "discovery":
                reading = db.execute(
                    "SELECT 1 FROM ingest_polls p JOIN ingest_polls target "
                    "ON target.link_id=p.link_id WHERE target.id=%s AND p.id<>target.id "
                    "AND NOT p.discovery_done",
                    (job["poll_id"],),
                ).fetchone()
                if reading:
                    raise CatalogError("O link já está em leitura por outra rodada.", 409)
                db.execute(
                    "UPDATE ingest_polls SET discovery_done=false WHERE id=%s",
                    (job["poll_id"],),
                )
            setting = db.execute("SELECT * FROM ingest_settings WHERE id=1").fetchone()
            db.execute(
                "UPDATE ingest_jobs SET "
                "state='retry',available_at=now(),dispatch=dispatch+1,owner=NULL,"
                "finished_at=NULL,failures=0,updated_at=now(),payload=payload||%s WHERE id=%s",
                (
                    Jsonb(
                        dict(
                            retry_config=setting["config"], retry_config_version=setting["version"]
                        )
                    ),
                    ident,
                ),
            )
            db.execute(
                "INSERT INTO ingest_outbox(job_id,dispatch,stage) SELECT id,dispatch,stage FROM "
                "ingest_jobs WHERE id=%s",
                (ident,),
            )
            db.execute(
                "UPDATE ingest_runs SET state='running',finished_at=NULL WHERE id=%s",
                (job["run_id"],),
            )
            if job["hash"]:
                db.execute(
                    "UPDATE ingest_articles SET status=%s WHERE hash=%s",
                    (job["stage"], job["hash"]),
                )
            db.execute(
                "INSERT INTO events(entity,action,happened_at,after_json) "
                "VALUES('ingest_job','retry',now(),%s)",
                (
                    Jsonb(
                        dict(
                            job_id=ident,
                            stage=job["stage"],
                            config_version=setting["version"],
                            config=setting["config"],
                        )
                    ),
                ),
            )
            return dict(id=ident, state="retry")

    def overview(
        self,
        *,
        offset: int = 0,
        stage: str = "",
        state: str = "",
        run_id: str = "",
        source_id: int = 0,
    ) -> dict[str, Any]:
        """Funil por notícia, filas por tarefa e paginação com os motivos individuais."""
        self.initialize()
        if (
            stage
            and stage not in STAGES
            or state
            and state not in ("pending", "queued", "running", "retry", *TERMINAL)
        ):
            raise CatalogError("Filtro de fila inválido.")
        if not 0 <= offset <= 1000000 or source_id < 0:
            raise CatalogError("Paginação inválida.")
        source_clause = (
            "(%s=0 OR (p.snapshot->'source'->>'id')::bigint=%s OR EXISTS "
            "(SELECT 1 FROM ingest_occurrences so JOIN ingest_polls sp ON sp.id=so.poll_id "
            "WHERE so.hash=j.hash AND so.run_id=j.run_id "
            "AND (sp.snapshot->'source'->>'id')::bigint=%s))"
        )
        with self.connection() as db:
            queues = db.execute(
                "SELECT j.stage,j.state,count(*) n,min(j.created_at) oldest FROM ingest_jobs j "
                "JOIN ingest_polls p ON p.id=j.poll_id WHERE (%s='' OR j.run_id=%s) "
                "AND " + source_clause + " GROUP BY j.stage,j.state",
                (run_id, run_id, source_id, source_id, source_id),
            ).fetchall()
            workers = db.execute(
                "SELECT * FROM ingest_workers WHERE heartbeat_at>now()-interval '1 "
                "day' ORDER BY role"
            ).fetchall()
            runs = db.execute(
                "SELECT r.*, "
                "(SELECT count(*) FROM ingest_jobs j WHERE j.run_id=r.id AND "
                "j.state IN ('pending','queued','retry','running') AND "
                "NOT (j.failures>0 OR j.payload @> '{\"recovery\":true}')) primary_pending,"
                "(SELECT count(*) FROM ingest_jobs j WHERE j.run_id=r.id AND "
                "j.state IN ('pending','queued','retry','running') AND "
                "(j.failures>0 OR j.payload @> '{\"recovery\":true}')) recovery_pending,"
                "(SELECT count(*) FROM ingest_polls p WHERE p.run_id=r.id) links,"
                "(SELECT count(*) FROM ingest_polls p WHERE p.run_id=r.id AND "
                "p.discovery_done) links_done,"
                "(SELECT count(*) FROM ingest_occurrences o WHERE o.run_id=r.id) occurrences,"
                "(SELECT count(DISTINCT hash) FROM ingest_occurrences o WHERE "
                "o.run_id=r.id) unique_articles "
                "FROM ingest_runs r ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            where = (
                "(%s='' OR j.stage=%s) AND (%s='' OR j.state=%s) AND (%s='' OR j.run_id=%s) "
                "AND " + source_clause
            )
            args = (stage, stage, state, state, run_id, run_id, source_id, source_id, source_id)
            count = db.execute(
                "SELECT count(*) n FROM ingest_jobs j JOIN ingest_polls p ON p.id=j.poll_id "
                "WHERE " + where,
                args,
            ).fetchone()["n"]
            jobs = db.execute(
                "SELECT j.*,p.link_id,p.snapshot->'source'->>'name' source,a.url article_url "
                "FROM ingest_jobs j JOIN ingest_polls p ON p.id=j.poll_id LEFT JOIN "
                "ingest_articles a ON a.hash=j.hash "
                "WHERE " + where + " ORDER BY j.created_at DESC LIMIT 25 OFFSET %s",
                (*args, offset),
            ).fetchall()
            for job in jobs:
                job["attempt_history"] = db.execute(
                    "SELECT * FROM ingest_attempts WHERE job_id=%s ORDER BY attempt", (job["id"],)
                ).fetchall()
            funnel = db.execute(
                "SELECT a.status,count(DISTINCT a.hash) n FROM ingest_articles a "
                "WHERE EXISTS(SELECT 1 FROM ingest_occurrences o JOIN ingest_polls p "
                "ON p.id=o.poll_id WHERE o.hash=a.hash AND (%s='' OR o.run_id=%s) "
                "AND (%s=0 OR (p.snapshot->'source'->>'id')::bigint=%s)) GROUP BY a.status",
                (run_id, run_id, source_id, source_id),
            ).fetchall()
            metrics = db.execute(
                "SELECT "
                "j.stage,sum(COALESCE((a.metrics->>'bright_data_requests')::int,0)) "
                "bright_data_requests,"
                "sum(COALESCE((a.metrics->>'prompt_tokens')::int,0)) prompt_tokens,"
                "sum(COALESCE((a.metrics->>'completion_tokens')::int,0)) completion_tokens,"
                "sum(COALESCE((a.metrics->>'avoided_article_requests')::int,0)) "
                "avoided_article_requests,"
                "sum(COALESCE((a.metrics->>'avoided_xml_requests')::int,0)) avoided_xml_requests,"
                "sum((a.metrics->>'cost_usd')::numeric) cost_usd FROM ingest_attempts "
                "a JOIN ingest_jobs j ON j.id=a.job_id JOIN ingest_polls p ON p.id=j.poll_id "
                "WHERE (%s='' OR j.run_id=%s) AND " + source_clause + " GROUP BY j.stage",
                (run_id, run_id, source_id, source_id, source_id),
            ).fetchall()
            return dict(
                settings=self.settings(),
                queues=queues,
                workers=workers,
                runs=runs,
                jobs=jobs,
                total=count,
                offset=offset,
                funnel=funnel,
                metrics=metrics,
            )
