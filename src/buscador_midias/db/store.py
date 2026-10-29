"""``WorkStore``: o store que o motor, o publicador e o agendador usam sobre ``bm_*``.

Entrega as tarefas no mesmo formato do store antigo (``snapshot``, ``run``, ``payload``,
``article``), então as seis etapas de ``ingestion/engine.py`` rodam sem mudança de lógica.
Cada operação é uma transação; o resultado de uma etapa, a próxima etapa, as URLs admitidas e
o fechamento da leitura são confirmados juntos.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db import run_stats
from buscador_midias.db.catalog import CatalogRepository
from buscador_midias.db.connection import Repository
from buscador_midias.db.discovery import (
    close_reads,
    complete_discovery,
    finish_runs,
    read_plan,
    save_plan,
)
from buscador_midias.db.queue import (
    ARTICLES,
    DISCOVERY,
    Queue,
    capacity,
    claim,
    complete,
    complete_many,
    fail,
    locked,
    maintain,
    renew_lease,
    reserve_for_publish,
)
from buscador_midias.db.runs import create_search_run, stop_runs
from buscador_midias.db.settings import SettingsRepository
from buscador_midias.domain.url import url_hash

RESERVE_BYTES = 256 * 1024 * 1024
LEASE_SECONDS = 300
_SNAPSHOT = (
    "SELECT l.id AS link_id,l.version AS link_version,l.url,l.kind,l.mechanism,"
    "l.access_transport,l.read_order,s.id AS site_id,s.name,s.url AS site_url,"
    "s.version AS site_version,s.country,s.language,m.version AS mechanism_version,"
    "m.parameters FROM bm_links l JOIN bm_sites s ON s.id=l.site_id "
    "JOIN bm_mechanisms m ON m.model=l.mechanism WHERE l.id=ANY(%s)"
)


def _queue(stage: str) -> Queue:
    return DISCOVERY if stage == "discovery" else ARTICLES


class WorkStore(Repository):
    """Reserva, conclui e falha tarefas; cria rodadas; entrega ao motor o que ele precisa."""

    def __init__(self, schema: str | None = None, env: Mapping[str, str] | None = None) -> None:
        """Os repositórios de cadastro e de configuração compartilham o schema e o ambiente."""
        super().__init__(schema, env)
        self.catalog = CatalogRepository(self.schema, env)
        self.config = SettingsRepository(self.schema, env)

    def install(self) -> None:
        """Instala tabelas, funções, mecanismos padrão e configurações; repetir é seguro."""
        self.catalog.install()
        self.config.install()

    def settings(self) -> dict[str, Any]:
        """Configuração atual da busca, no formato ``{"version", "config"}``."""
        current = self.config.search()
        return dict(version=current["version"], config=current["config"])

    def lease_connection(self) -> Any:
        """Conexão exclusiva para locks que atravessam um HTTP; não disputa a dos checkpoints."""
        return self.direct_connection()

    # --- rodadas ---------------------------------------------------------------------------

    def create_run(self, data: dict[str, Any], *, scheduled: bool = False) -> dict[str, Any] | None:
        """Cria a rodada de busca (ver ``create_search_run``); ``None`` se não há o que fazer."""
        config = self.config.search()["config"]
        with self.connection() as db:
            return create_search_run(db, data, config=config, scheduled=scheduled)

    # --- reservar, concluir, falhar --------------------------------------------------------

    def claim_many(
        self, stage: str, keys: list[Any], owner: str, *, lane: str | None = None
    ) -> list[dict[str, Any]]:
        """Reserva um lote e devolve as tarefas no formato do motor."""
        queue = _queue(stage)
        with self.connection() as db:
            rows = claim(db, stage, keys, owner, lane=lane, queue=queue)
            return self._jobs(db, rows, queue)

    def claim(
        self, stage: str, key: Any, owner: str, *, lane: str | None = None
    ) -> dict[str, Any] | None:
        """Reserva uma tarefa; ``None`` se a mensagem era repetida ou velha."""
        jobs = self.claim_many(stage, [key], owner, lane=lane)
        return jobs[0] if jobs else None

    def finish(self, job: dict[str, Any], output: dict[str, Any]) -> bool:
        """Confirma o resultado de uma etapa de forma atômica; ``False`` se a reserva é velha."""
        with self.connection() as db:
            if job["stage"] == "discovery":
                return complete_discovery(db, job, output)
            return complete(db, job, output)

    def finish_many(self, jobs: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> None:
        """Conclui um lote na mesma transação (o dedup devolve centenas de notícias por vez)."""
        if len(jobs) != len(outputs):
            raise ValueError("Quantidade de resultados diferente da quantidade de tarefas")
        with self.connection() as db:
            articles = [
                (j, o) for j, o in zip(jobs, outputs, strict=True) if j["stage"] != "discovery"
            ]
            if articles:
                complete_many(db, [j for j, _ in articles], [o for _, o in articles])
            for job, output in zip(jobs, outputs, strict=True):
                if job["stage"] == "discovery":
                    complete_discovery(db, job, output)

    def failure(
        self,
        job: dict[str, Any],
        reason: str,
        *,
        busy: bool = False,
        metrics: dict[str, Any] | None = None,
        defer_seconds: int = 5,
        recovery: bool = False,
        retryable: bool = True,
    ) -> str:
        """Espera crescente e limite de tentativas; ``"retry"``, ``"failed"`` ou ``"stale"``."""
        with self.connection() as db:
            return fail(
                db,
                job,
                reason,
                busy=busy,
                metrics=metrics,
                defer_seconds=defer_seconds,
                recovery=recovery,
                retryable=retryable,
                queue=_queue(job["stage"]),
            )

    def renew_lease(self, job: dict[str, Any]) -> bool:
        """Estende a reserva de uma tarefa em andamento; ``False`` se já não é do consumidor."""
        with self.connection() as db:
            return renew_lease(db, job, queue=_queue(job["stage"]))

    def save_entity_context_progress(self, job: dict[str, Any], progress: dict[str, Any]) -> None:
        """Persiste um lote de contexto e renova a reserva sem concluir a etapa."""
        with self.connection() as db:
            if locked(db, job) is None:
                raise CatalogError("Reserva de análise substituída; checkpoint não aplicado", 409)
            db.execute(
                "UPDATE bm_articles SET payload=payload||%s,"
                "locked_until=now()+%s*interval '1 second',updated_at=now() WHERE hash=%s",
                (Jsonb(dict(entity_context_progress=progress)), LEASE_SECONDS, job["hash"]),
            )

    # --- manutenção e publicação -----------------------------------------------------------

    def maintain(self) -> dict[str, int]:
        """Recupera reservas, republica o que se perdeu, fecha leituras e rodadas."""
        with self.connection() as db:
            articles = maintain(db)
            tasks = maintain(db, queue=DISCOVERY)
            reads = close_reads(db)
            runs = finish_runs(db)
            run_stats.finalize_pending(db, 2)
        return dict(
            expired=articles.expired + tasks.expired,
            republished=articles.republished + tasks.republished,
            auto_retried=articles.auto_retried,
            reads_closed=reads,
            runs_closed=runs,
        )

    def stop_runs(self) -> dict[str, Any]:
        """Cancela o trabalho ainda não analisado das rodadas abertas (ver ``stop_runs``)."""
        with self.connection() as db:
            return stop_runs(db)

    def capacity(self, stage: str) -> tuple[bool, str]:
        """Contém produtores antes de gerar mais trabalho; consumidores finais continuam."""
        if stage in ("discovery", "fetch") and shutil.disk_usage(Path.cwd()).free < RESERVE_BYTES:
            return False, "Reserva de disco: aguardando liberação de espaço"
        with self.connection() as db:
            if not capacity(db, stage):
                return False, f"Aguardando escoamento depois da etapa {stage}"
        return True, ""

    def publish(
        self,
        stage: str,
        lane: str,
        limit: int,
        *,
        per_host: int | None = None,
        per_host_max: int | None = None,
        send: Callable[[list[Any]], None] | None = None,
    ) -> list[Any]:
        """Reserva as tarefas mais antigas, revezando entre hosts, e as entrega a ``send``.

        A reserva e o envio acontecem na mesma transação: se o broker falhar, nada fica marcado
        como enfileirado. Mensagem duplicada por uma queda depois do envio é inofensiva, porque
        o consumidor reserva por compare-and-set.
        """
        with self.connection() as db:
            keys = reserve_for_publish(
                db,
                stage,
                lane,
                limit=limit,
                per_host=per_host,
                per_host_max=per_host_max,
                queue=_queue(stage),
            )
            if keys and send is not None:
                send(keys)
            return keys

    # --- pré-filtro de janela ---------------------------------------------------------------

    def window_candidates(self, cursor: str = "") -> list[dict[str, Any]]:
        """Notícias já esperando que podem ser fechadas por data, sem esperar a fila nem HTTP."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT a.hash,a.stage,a.url,a.payload,a.site_id,r.since,r.until,"
                "r.config->'mechanisms'->l.mechanism->'parameters' AS parameters "
                "FROM bm_articles a JOIN bm_runs r ON r.id=a.run_id "
                "LEFT JOIN bm_links l ON l.id=a.link_id "
                "WHERE a.hash>%s AND a.stage IN ('dedup','fetch') "
                "AND a.state IN ('pending','queued','retry') AND a.next_attempt_at<=now() "
                "AND (a.url ~ '/20[0-9]{2}/[0-9]{2}/[0-9]{2}' "
                "OR a.payload->>'published_hint_basis'='xml_publication') "
                "ORDER BY a.hash LIMIT 1000",
                (cursor,),
            ).fetchall()
        return [
            dict(
                id=r["hash"],
                stage=r["stage"],
                snapshot=dict(source=dict(id=r["site_id"]), parameters=r["parameters"] or {}),
                article=dict(url=r["url"], payload=r["payload"]),
                run=dict(since=r["since"], until=r["until"]),
            )
            for r in rows
        ]

    def apply_decisions(self, decisions: list[tuple[str, str, dict[str, Any]]], owner: str) -> int:
        """Fecha as notícias decididas ``(etapa, hash, saída)``; ignora as que já seguiram."""
        done = 0
        for stage in dict.fromkeys(d[0] for d in decisions):
            chosen = {key: output for s, key, output in decisions if s == stage}
            jobs = self.claim_many(stage, list(chosen), owner)
            if not jobs:
                continue
            self.finish_many(jobs, [chosen[j["id"]] for j in jobs])
            done += len(jobs)
        return done

    # --- o que o motor pede ao store -------------------------------------------------------

    def current_versions(self, link_id: int) -> dict[str, Any] | None:
        """Estado atual do cadastro de um link, para detectar edição durante a rodada."""
        with self.connection() as db:
            row = db.execute(
                "SELECT l.active,l.deleted_at,l.version,s.active AS sa,s.deleted_at AS sd,"
                "s.version AS sv,m.version AS mv FROM bm_links l JOIN bm_sites s ON s.id=l.site_id "
                "JOIN bm_mechanisms m ON m.model=l.mechanism WHERE l.id=%s",
                (link_id,),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def host_cooldown_seconds(lock_db: Any, host: str) -> int | None:
        """Segundos até o host poder ser lido de novo (``Retry-After``), se estiver em espera."""
        row = lock_db.execute(
            "SELECT ceil(extract(epoch FROM cooldown_until-now()))::bigint AS seconds "
            "FROM bm_host_state WHERE host=%s AND cooldown_until>now()",
            (host,),
        ).fetchone()
        return int(row["seconds"]) if row else None

    def route_observations(self, source_id: int) -> list[dict[str, Any]]:
        """Amostras (documento, notícia, data) de que as rotas temporais de uma fonte aprendem."""
        with self.connection() as db:
            return db.execute(
                "SELECT l.url,x.sample FROM bm_links l JOIN bm_link_state s ON s.link_id=l.id "
                "CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN "
                "jsonb_typeof(s.last_test->'samples')='array' THEN s.last_test->'samples' "
                "ELSE '[]'::jsonb END) x(sample) WHERE l.site_id=%(site)s AND "
                "l.deleted_at IS NULL AND s.last_test->>'usable_for_news'='true' "
                "UNION ALL (SELECT a.document_url AS url,jsonb_build_object('url',a.url,"
                "'text',true,'published_at',a.payload->>'published_at') AS sample "
                "FROM bm_articles a WHERE a.site_id=%(site)s AND a.document_url IS NOT NULL "
                "AND a.payload->>'published_at_basis'='html' "
                "AND a.payload->>'published_at' IS NOT NULL LIMIT 500)",
                dict(site=source_id),
            ).fetchall()

    def demote_read_order(self, link_id: int, found: str) -> None:
        """Corrige ``newest_first`` que o próprio documento contrariou; nunca promove."""
        with self.connection() as db:
            db.execute(
                "UPDATE bm_links SET read_order=%s WHERE id=%s AND read_order='newest_first'",
                (found, link_id),
            )

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
        """XML interpretado para revalidação condicional; não dispensa novo acesso."""
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM bm_document_cache WHERE site_id=%s AND url_hash=%s AND revision=%s",
                self.document_identity(job),
            ).fetchone()
        if row:
            return dict(row, document=json.loads(gzip.decompress(row["document"])))
        return None

    def cache_document(self, job: dict[str, Any], document: Any, response: Any) -> None:
        """Preserva o XML interpretado; a seleção de dias é refeita a cada janela."""
        with self.connection() as db:
            db.execute(
                "INSERT INTO bm_document_cache(site_id,url_hash,revision,url,content_hash,etag,"
                "last_modified,document) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(site_id,url_hash,revision) DO UPDATE SET "
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

    def save_plan(self, job: dict[str, Any], plan: dict[str, Any]) -> str:
        """Guarda o documento já interpretado na própria tarefa de descoberta."""
        with self.connection() as db:
            return save_plan(db, job, plan)

    def read_plan(self, key: str) -> dict[str, Any]:
        """Recupera o plano sem repetir HTTP nem a expansão recursiva do documento."""
        with self.connection() as db:
            return read_plan(db, key)

    # --- formato de tarefa do motor --------------------------------------------------------

    def _jobs(
        self, db: psycopg.Connection[Any], rows: list[dict[str, Any]], queue: Queue
    ) -> list[dict[str, Any]]:
        """Acrescenta ``snapshot``, ``payload`` e ``article`` como o motor espera."""
        if not rows:
            return []
        link_ids = list({r["link_id"] for r in rows if r.get("link_id")})
        links = {r["link_id"]: r for r in db.execute(_SNAPSHOT, (link_ids,)).fetchall()}
        frozen = {}
        if queue is DISCOVERY:
            for r in db.execute(
                "SELECT run_id,link_id,link_version,site_version,mechanism_version "
                "FROM bm_run_links WHERE (run_id,link_id) IN "
                "(SELECT unnest(%s::text[]),unnest(%s::bigint[]))",
                ([r["run_id"] for r in rows], [r["link_id"] for r in rows]),
            ).fetchall():
                frozen[(r["run_id"], r["link_id"])] = r
        jobs = []
        for row in rows:
            link = links.get(row["link_id"])
            if link is None:
                raise CatalogError("O link desta tarefa deixou de existir.", 409)
            versions = frozen.get((row["run_id"], row["link_id"]))
            model = link["mechanism"]
            pinned = row["run"]["config"].get("mechanisms", {}).get(model)
            snapshot = dict(
                link_id=link["link_id"],
                link_version=versions["link_version"] if versions else link["link_version"],
                model=model,
                model_version=(
                    versions["mechanism_version"] if versions else link["mechanism_version"]
                ),
                parameters=pinned["parameters"] if pinned else link["parameters"],
                url=link["url"],
                kind=link["kind"],
                access_transport=link["access_transport"],
                read_order=link["read_order"],
                source=dict(
                    id=link["site_id"],
                    name=link["name"],
                    url=link["site_url"],
                    version=versions["site_version"] if versions else link["site_version"],
                    country=link["country"],
                    language=link["language"],
                ),
            )
            if queue is DISCOVERY:
                payload: dict[str, Any] = dict(url=row["url"], depth=row["depth"])
                if row["slice"]:
                    payload["cursor"] = row["slice"]
                if row["plan_ref"]:
                    payload["plan_key"] = str(row["plan_ref"])
                jobs.append(
                    dict(
                        row,
                        snapshot=snapshot,
                        payload=payload,
                        hash=None,
                        generation=1,
                        failures=max(0, row["attempts"] - 1),
                    )
                )
            else:
                jobs.append(
                    dict(
                        row,
                        snapshot=snapshot,
                        payload={},
                        article=row,
                        generation=1,
                        failures=max(0, row["attempts"] - 1),
                    )
                )
        return jobs
