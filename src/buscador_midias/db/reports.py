"""Consultas de leitura das telas de busca (filas, rodadas, sites, notícias, custos e pendências).

Cada notícia é uma linha só em ``bm_articles``; a etapa e a situação atuais, a origem, o custo e o
motivo da falha estão nela. As consultas daqui só agregam e filtram essa linha (e as tarefas de
descoberta), sem reconstruir o histórico a partir de várias tabelas.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg import sql

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.connection import Repository
from buscador_midias.db.queue import retry_failed
from buscador_midias.db.settings import STAGES, SettingsRepository

PAGE = 25
ARTICLE_STAGES = ("dedup", "fetch", "context", "evidence", "persist")
STATES = ("pending", "queued", "running", "retry", "succeeded", "failed", "cancelled")
REASON_CLASSES = ("transient", "blocked", "permanent", "bug")
OPEN = "('pending','queued','running','retry')"
_RUN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_STATE = "CASE a.state WHEN 'done' THEN 'succeeded' ELSE a.state END"
_TASK_STATE = "CASE t.state WHEN 'done' THEN 'succeeded' ELSE t.state END"
# Onde cada notícia está: o resultado final, ou a etapa em que ainda espera.
_STATUS = (
    "CASE a.state WHEN 'done' THEN a.outcome WHEN 'failed' THEN 'failed' "
    "WHEN 'cancelled' THEN 'cancelled' ELSE a.stage END"
)
ACTIVE = {*ARTICLE_STAGES}
STEPS = {"dedup": 0, "fetch": 1, "context": 2, "evidence": 3, "persist": 4}
_RETRY_KEYS = {"stage", "reason", "reason_class", "run_id", "source_id", "hashes", "everything"}
_COSTS = (
    "bright_data_requests",
    "prompt_tokens",
    "completion_tokens",
    "avoided_article_requests",
    "avoided_xml_requests",
)


def summarize(
    source: dict[str, Any], statuses: dict[str, int], queues: list[Any]
) -> dict[str, Any]:
    """Exibe conclusão com pendências como 100% encerrado, sem confundir com sucesso."""
    links = int(source["links"])
    links_done = int(source["links_done"])
    articles = sum(statuses.values())
    total = links + articles * 5
    done = links_done + sum(STEPS.get(status, 5) * n for status, n in statuses.items())
    in_discovery = links_done < links
    waiting = sum(n for status, n in statuses.items() if status in ACTIVE)
    open_jobs = sum(
        int(q["n"]) for q in queues if q["state"] in ("pending", "queued", "running", "retry")
    )
    errors = statuses.get("failed", 0)
    closed = not in_discovery and waiting == 0 and open_jobs == 0
    progress = min(99, int(done * 100 / total)) if total and not closed else 100 if closed else 0
    return dict(
        source,
        articles=articles,
        statuses=statuses,
        stages=queues,
        errors=errors,
        current_errors=errors,
        historical_errors=0,
        inherited_failures=[],
        active_articles=waiting,
        discovery_complete=not in_discovery,
        progress=None if in_discovery else progress,
        state="partial" if closed and errors else "completed" if closed else "running",
    )


class ReportRepository(Repository):
    """Leituras e reabertura de pendências; não cria rodadas nem altera o cadastro."""

    def __init__(
        self,
        schema: str | None = None,
        env: Mapping[str, str] | None = None,
        *,
        corpus_schema: str = "public",
    ) -> None:
        """``corpus_schema`` é onde mora ``midias_ia_articles`` (o corpus do PowerDD/KyX)."""
        super().__init__(schema, env)
        self.corpus_schema = corpus_schema

    # --- filas e notícias ----------------------------------------------------------------------

    def overview(
        self,
        *,
        offset: int = 0,
        stage: str = "",
        state: str = "",
        run_id: str = "",
        source_id: int = 0,
    ) -> dict[str, Any]:
        """Filas por etapa, rodadas, funil e a página de notícias (ou leituras) do filtro."""
        if (stage and stage not in STAGES) or (state and state not in STATES):
            raise CatalogError("Filtro de fila inválido.")
        if not 0 <= offset <= 1_000_000 or source_id < 0:
            raise CatalogError("Paginação inválida.")
        with self.connection() as db:
            run_id = self._resolve_run(db, run_id)
            where, args = self._article_scope(run_id, source_id)
            task_where, task_args = self._task_scope(run_id, source_id)
            queues = db.execute(
                f"SELECT a.stage,{_STATE} state,count(*) n,min(a.created_at) oldest "  # noqa: S608
                f"FROM bm_articles a WHERE {where} GROUP BY 1,2 UNION ALL "
                f"SELECT 'discovery',{_TASK_STATE},count(*),min(t.created_at) FROM "  # noqa: S608
                f"bm_discovery_tasks t JOIN bm_run_links rl ON rl.run_id=t.run_id "
                f"AND rl.link_id=t.link_id WHERE {task_where} GROUP BY 1,2",
                (*args, *task_args),
            ).fetchall()
            funnel = db.execute(
                f"SELECT {_STATUS} status,count(*) n FROM bm_articles a WHERE {where} GROUP BY 1",  # noqa: S608
                args,
            ).fetchall()
            items, total = (
                self._tasks(db, task_where, task_args, state, offset)
                if stage == "discovery"
                else self._articles(db, where, args, stage, state, offset)
            )
            runs = self._runs(db)
        return dict(
            settings=SettingsRepository(self.schema, self.env).search(),
            run_id=run_id,
            queues=queues,
            runs=runs,
            items=items,
            total=total,
            offset=offset,
            funnel=funnel,
        )

    def metrics(self, *, run_id: str = "", source_id: int = 0) -> list[dict[str, Any]]:
        """Chamadas pagas, tokens e custo por etapa. Fica à parte porque soma todas as notícias."""
        if source_id < 0:
            raise CatalogError("Paginação inválida.")
        sums = (
            ",".join(f"sum(COALESCE((e.v->>'{key}')::numeric,0)) {key}" for key in _COSTS)
            + ",sum((e.v->>'cost_usd')::numeric) cost_usd"
        )
        with self.connection() as db:
            run_id = self._resolve_run(db, run_id)
            where, args = self._article_scope(run_id, source_id)
            task_where, task_args = self._task_scope(run_id, source_id)
            rows = db.execute(
                f"SELECT e.stage,{sums} FROM bm_articles a "  # noqa: S608
                f"CROSS JOIN LATERAL jsonb_each(a.cost) e(stage,v) WHERE {where} GROUP BY e.stage",
                args,
            ).fetchall()
            discovery = db.execute(
                f"SELECT 'discovery' stage,{sums} FROM bm_discovery_tasks t "  # noqa: S608
                "JOIN bm_run_links rl ON rl.run_id=t.run_id AND rl.link_id=t.link_id "
                "CROSS JOIN LATERAL (SELECT 'discovery' stage,t.result->'metrics' v) e "
                f"WHERE jsonb_typeof(t.result->'metrics')='object' AND {task_where}",  # noqa: S608
                task_args,
            ).fetchone()
        if discovery and any(discovery[key] for key in _COSTS):
            rows.append(discovery)
        return rows

    def article(self, ident: str) -> dict[str, Any]:
        """A notícia com a origem (primeira listagem) e o corpo gravado no corpus, se houver."""
        with self.connection() as db:
            article = db.execute(
                "SELECT a.*,s.name source_name,l.url link_url FROM bm_articles a "
                "JOIN midias_ia_trusted_sources s ON s.id=a.site_id LEFT JOIN bm_links l ON l.id=a.link_id "
                "WHERE a.hash=%s",
                (ident,),
            ).fetchone()
            if not article:
                raise CatalogError("Notícia não encontrada.", 404)
            if article["corpus_article_id"] and not article["payload"].get("body"):
                corpus = self._corpus_row(db, article["corpus_article_id"])
                if corpus:
                    article["payload"]["body"] = corpus["body"]
        origin = dict(
            source=article.pop("source_name"),
            site_id=article["site_id"],
            link_id=article["link_id"],
            link_url=article.pop("link_url"),
            document_url=article["document_url"],
            first_run_id=article["first_run_id"],
            run_id=article["run_id"],
            first_seen_at=article["first_seen_at"],
            last_seen_at=article["last_seen_at"],
            seen_count=article["seen_count"],
        )
        return dict(article=article, origin=origin)

    def evidence(self, ident: str) -> dict[str, Any] | None:
        """Caminho e situação da evidência do registro do corpus ligado à notícia."""
        if not re.fullmatch(r"[a-f0-9]{64}", ident):
            return None
        with self.connection() as db:
            return db.execute(
                sql.SQL(
                    "SELECT c.evidencia_path path,c.evidencia_status status FROM bm_articles a "
                    "JOIN {}.midias_ia_articles c ON c.id=a.corpus_article_id WHERE a.hash=%s"
                ).format(sql.Identifier(self.corpus_schema)),
                (ident,),
            ).fetchone()

    # --- sites ---------------------------------------------------------------------------------

    def source_progress(
        self, run_id: str = "", offset: int = 0, search: str = ""
    ) -> dict[str, Any]:
        """Uma linha por site da rodada, sem contar a mesma notícia duas vezes."""
        if not 0 <= offset <= 1_000_000:
            raise CatalogError("Rodada ou paginação inválida.")
        with self.connection() as db:
            resolved = self._resolve_run(db, run_id or "latest")
            run = (
                db.execute("SELECT * FROM bm_runs WHERE id=%s", (resolved,)).fetchone()
                if resolved
                else None
            )
            if run is None:
                if run_id:
                    raise CatalogError("Rodada não encontrada.", 404)
                return dict(run=None, sources=[], total=0, offset=offset, summary={})
            sites = db.execute(
                "SELECT s.id,s.name,s.url,count(*) links,count(*) FILTER (WHERE rl.discovery_done) "
                "links_done FROM bm_run_links rl JOIN midias_ia_trusted_sources s ON s.id=rl.site_id "
                "WHERE rl.run_id=%s GROUP BY s.id,s.name,s.url",
                (resolved,),
            ).fetchall()
            statuses = db.execute(
                f"SELECT a.site_id,{_STATUS} status,count(*) n FROM bm_articles a "  # noqa: S608
                "WHERE a.run_id=%s GROUP BY 1,2",
                (resolved,),
            ).fetchall()
            queues = db.execute(
                f"SELECT a.site_id,a.stage,{_STATE} state,count(*) n FROM bm_articles a "  # noqa: S608
                "WHERE a.run_id=%(run)s GROUP BY 1,2,3 UNION ALL "
                f"SELECT rl.site_id,'discovery',{_TASK_STATE},count(*) FROM bm_discovery_tasks t "  # noqa: S608
                "JOIN bm_run_links rl ON rl.run_id=t.run_id AND rl.link_id=t.link_id "
                "WHERE t.run_id=%(run)s GROUP BY 1,2,3",
                dict(run=resolved),
            ).fetchall()
        by_site: dict[int, dict[str, int]] = defaultdict(dict)
        stage_rows: dict[int, list[Any]] = defaultdict(list)
        for row in statuses:
            by_site[row["site_id"]][row["status"]] = int(row["n"])
        for row in queues:
            stage_rows[row["site_id"]].append(
                dict(stage=row["stage"], state=row["state"], n=row["n"])
            )
        rows = [summarize(dict(s), by_site[s["id"]], stage_rows[s["id"]]) for s in sites]
        summary = dict(Counter(row["state"] for row in rows))
        shown = sorted(
            (r for r in rows if search.casefold() in r["name"].casefold()),
            key=lambda r: (r["state"] == "completed", r["name"].casefold()),
        )
        return dict(
            run=dict(run),
            sources=shown[offset : offset + PAGE],
            total=len(shown),
            offset=offset,
            summary=summary,
            stage_names=list(STAGES),
        )

    def corpus_articles(self, source_id: int, run_id: str, offset: int = 0) -> dict[str, Any]:
        """Registros reais do corpus encontrados pelo site na rodada indicada."""
        if source_id < 1 or not _RUN.fullmatch(run_id) or not 0 <= offset <= 1_000_000:
            raise CatalogError("Fonte, rodada ou paginação inválida.")
        with self.connection() as db:
            site = db.execute(
                "SELECT s.name FROM bm_run_links rl JOIN midias_ia_trusted_sources s ON s.id=rl.site_id "
                "WHERE rl.run_id=%s AND rl.site_id=%s LIMIT 1",
                (run_id, source_id),
            ).fetchone()
            if not site:
                raise CatalogError("Site não encontrado nesta rodada.", 404)
            relation = sql.SQL(
                "FROM bm_articles a JOIN {}.midias_ia_articles c ON c.id=a.corpus_article_id "
                "WHERE a.run_id=%s AND a.site_id=%s"
            ).format(sql.Identifier(self.corpus_schema))
            total = db.execute(
                sql.SQL("SELECT count(DISTINCT c.id) n ") + relation, (run_id, source_id)
            ).fetchone()
            rows = db.execute(
                sql.SQL(
                    "SELECT DISTINCT ON(c.id) c.id,a.hash,c.titulo title,c.url,c.published_at,"
                    "c.evidencia_status evidence_status,c.evidencia_path "
                )
                + relation
                + sql.SQL(" ORDER BY c.id DESC,a.hash LIMIT 25 OFFSET %s"),
                (run_id, source_id, offset),
            ).fetchall()
        assert total
        for row in rows:
            row["evidence_url"] = (
                "/evidencias/corpus/" + row["hash"]
                if row["evidence_status"] == "captured" and row["evidencia_path"]
                else None
            )
        return dict(
            source_id=source_id,
            source=site["name"],
            run_id=run_id,
            articles=rows,
            total=total["n"],
            offset=offset,
        )

    # --- pendências ----------------------------------------------------------------------------

    def pending(
        self,
        *,
        offset: int = 0,
        stage: str = "",
        reason: str = "",
        reason_class: str = "",
        run_id: str = "",
        source_id: int = 0,
    ) -> dict[str, Any]:
        """Notícias que falharam, agrupadas por etapa, motivo e classe, e a página do filtro."""
        if (
            (stage and stage not in ARTICLE_STAGES)
            or (reason_class and reason_class not in REASON_CLASSES)
            or not 0 <= offset <= 1_000_000
            or source_id < 0
        ):
            raise CatalogError("Filtro de pendências inválido.")
        where = "a.state='failed'"
        args: list[Any] = []
        for condition, value in (
            ("a.failed_stage=%s", stage),
            ("a.reason=%s", reason),
            ("a.reason_class=%s", reason_class),
            ("a.run_id=%s", run_id),
            ("a.site_id=%s", source_id or None),
        ):
            if value:
                where += f" AND {condition}"
                args.append(value)
        with self.connection() as db:
            groups = db.execute(
                "SELECT a.failed_stage,a.reason,a.reason_class,bool_or(a.retryable) retryable,"  # noqa: S608
                f"count(*) n,max(a.failed_at) last_at FROM bm_articles a WHERE {where} "
                "GROUP BY 1,2,3 ORDER BY n DESC,1,2 LIMIT 50",
                args,
            ).fetchall()
            total = db.execute(
                f"SELECT count(*) n FROM bm_articles a WHERE {where}",  # noqa: S608
                args,
            ).fetchone()
            items = db.execute(
                f"{self._item_select()} WHERE {where} "  # noqa: S608
                "ORDER BY a.failed_at DESC,a.hash LIMIT %s OFFSET %s",
                (*args, PAGE, offset),
            ).fetchall()
        assert total
        return dict(groups=groups, items=items, total=total["n"], offset=offset)

    def retry(self, filters: dict[str, Any]) -> int:
        """Reabre as falhas escolhidas por uma pessoa; devolve quantas voltaram para a fila.

        Sem nenhum filtro só com ``everything=True``, para que um clique sem querer não reabra
        todas as pendências.
        """
        if filters.keys() - _RETRY_KEYS:
            raise CatalogError("Filtro de reprocessamento inválido.")
        stage = filters.get("stage") or None
        klass = filters.get("reason_class") or None
        hashes = filters.get("hashes")
        source = filters.get("source_id")
        if (
            (stage and stage not in ARTICLE_STAGES)
            or (klass and klass not in REASON_CLASSES)
            or (hashes is not None and not isinstance(hashes, list))
            or (source is not None and (type(source) is not int or source < 1))
            or any(
                filters.get(k) is not None and not isinstance(filters[k], str)
                for k in ("reason", "run_id")
            )
        ):
            raise CatalogError("Filtro de reprocessamento inválido.")
        chosen = {k: v for k, v in filters.items() if k != "everything" and v}
        if not chosen and filters.get("everything") is not True:
            raise CatalogError("Informe o que reprocessar ou confirme todas as pendências.")
        with self.connection() as db:
            return retry_failed(
                db,
                stage=stage,
                reason=filters.get("reason") or None,
                reason_class=klass,
                run_id=filters.get("run_id") or None,
                site_id=source,
                hashes=hashes or None,
            )

    # --- painel --------------------------------------------------------------------------------

    def panel(self, days: int = 7, **extra: Any) -> dict[str, Any]:
        """Tudo o que a tela Painel mostra para a janela de ``days`` dias."""
        from buscador_midias.db.panel import build_panel

        return build_panel(self, days, corpus_schema=self.corpus_schema, **extra)

    # --- internos ------------------------------------------------------------------------------

    def _resolve_run(self, db: psycopg.Connection[dict[str, Any]], run_id: str) -> str:
        """``latest`` vira a rodada de busca mais recente; vazio mantém o histórico inteiro."""
        if run_id == "latest":
            row = db.execute(
                "SELECT id FROM bm_runs WHERE type='search' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return str(row["id"]) if row else ""
        if run_id and not _RUN.fullmatch(run_id):
            raise CatalogError("Rodada inválida.")
        return run_id

    @staticmethod
    def _article_scope(run_id: str, source_id: int) -> tuple[str, list[Any]]:
        where: list[str] = ["TRUE"]
        args: list[Any] = []
        if run_id:
            where.append("a.run_id=%s")
            args.append(run_id)
        if source_id:
            where.append("a.site_id=%s")
            args.append(source_id)
        return " AND ".join(where), args

    @staticmethod
    def _task_scope(run_id: str, source_id: int) -> tuple[str, list[Any]]:
        where: list[str] = ["TRUE"]
        args: list[Any] = []
        if run_id:
            where.append("t.run_id=%s")
            args.append(run_id)
        if source_id:
            where.append("rl.site_id=%s")
            args.append(source_id)
        return " AND ".join(where), args

    @staticmethod
    def _item_select() -> str:
        return (
            f"SELECT a.hash id,'article' kind,a.url,s.name source,a.site_id,a.link_id,a.stage,"
            f"{_STATE} state,a.outcome,a.attempts,a.passes,a.failed_stage,a.reason,"
            "a.reason_class,a.retryable,a.failed_at,a.detail,a.lane,a.history,"
            "a.payload->>'title' title,a.run_id,a.created_at,a.updated_at FROM bm_articles a "
            "JOIN midias_ia_trusted_sources s ON s.id=a.site_id"
        )

    def _articles(
        self,
        db: psycopg.Connection[dict[str, Any]],
        where: str,
        args: list[Any],
        stage: str,
        state: str,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        if stage:
            where += " AND a.stage=%s"
            args = [*args, stage]
        if state:
            where += " AND a.state=%s"
            args = [*args, "done" if state == "succeeded" else state]
        total = db.execute(f"SELECT count(*) n FROM bm_articles a WHERE {where}", args).fetchone()  # noqa: S608
        items = db.execute(
            f"{self._item_select()} WHERE {where} "  # noqa: S608
            "ORDER BY a.updated_at DESC,a.hash LIMIT %s OFFSET %s",
            (*args, PAGE, offset),
        ).fetchall()
        assert total
        return items, int(total["n"])

    @staticmethod
    def _tasks(
        db: psycopg.Connection[dict[str, Any]],
        where: str,
        args: list[Any],
        state: str,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        if state:
            where += " AND t.state=%s"
            args = [*args, "done" if state == "succeeded" else state]
        base = (
            "FROM bm_discovery_tasks t JOIN bm_run_links rl ON rl.run_id=t.run_id "
            "AND rl.link_id=t.link_id JOIN midias_ia_trusted_sources s ON s.id=rl.site_id "
            f"WHERE {where}"  # noqa: S608
        )
        total = db.execute(f"SELECT count(*) n {base}", args).fetchone()  # noqa: S608
        items = db.execute(
            "SELECT t.id::text id,'discovery' kind,t.url,s.name source,rl.site_id site_id,"  # noqa: S608
            f"t.link_id,'discovery' stage,{_TASK_STATE} state,t.outcome,t.attempts,1 passes,"
            "t.failed_stage,t.reason,t.reason_class,t.retryable,t.failed_at,t.detail,t.lane,"
            "t.history,NULL title,t.run_id,t.created_at,t.updated_at "
            f"{base} ORDER BY t.updated_at DESC,t.id LIMIT %s OFFSET %s",
            (*args, PAGE, offset),
        ).fetchall()
        assert total
        return items, int(total["n"])

    @staticmethod
    def _runs(db: psycopg.Connection[dict[str, Any]]) -> list[dict[str, Any]]:
        return db.execute(
            "SELECT r.id,r.created_at,r.since,r.until,r.status state,r.finished_at,"
            "COALESCE(l.links,0) links,COALESCE(l.done,0) links_done,COALESCE(l.occ,0) occurrences,"
            "COALESCE(u.n,0) unique_articles,COALESCE(p.primary_pending,0) primary_pending,"
            "COALESCE(p.recovery_pending,0) recovery_pending FROM bm_runs r "
            "LEFT JOIN LATERAL (SELECT count(*)::int links,"
            "(count(*) FILTER (WHERE discovery_done))::int done,COALESCE(sum(listed),0)::int occ "
            "FROM bm_run_links WHERE run_id=r.id) l ON true "
            "LEFT JOIN LATERAL (SELECT count(*)::int n FROM bm_articles "
            "WHERE run_id=r.id) u ON true "
            "LEFT JOIN LATERAL (SELECT (count(*) FILTER (WHERE lane='primary'))::int "
            "primary_pending,(count(*) FILTER (WHERE lane='recovery'))::int recovery_pending "
            f"FROM (SELECT lane FROM bm_articles WHERE run_id=r.id AND state IN {OPEN} "  # noqa: S608
            "UNION ALL SELECT lane FROM bm_discovery_tasks WHERE run_id=r.id "
            f"AND state IN {OPEN}) x"
            ") p ON true WHERE r.type='search' ORDER BY r.created_at DESC LIMIT 20"
        ).fetchall()

    def _corpus_row(
        self, db: psycopg.Connection[dict[str, Any]], ident: int
    ) -> dict[str, Any] | None:
        table = f'"{self.corpus_schema}".midias_ia_articles'
        exists = db.execute("SELECT to_regclass(%s) IS NOT NULL ok", (table,)).fetchone()
        if not exists or not exists["ok"]:
            return None
        return db.execute(
            sql.SQL("SELECT body FROM {}.midias_ia_articles WHERE id=%s").format(
                sql.Identifier(self.corpus_schema)
            ),
            (ident,),
        ).fetchone()
