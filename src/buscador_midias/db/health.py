"""Rodadas de saúde (``bm_runs`` com ``type='health'``): fila finita, histórico e decisões.

Busca e saúde são o mesmo conceito, uma rodada com itens por link; só muda o ``type``. A rodada
congela a configuração (mecanismos incluídos) e a revisão de cada link, site e mecanismo. O
resultado de cada link fica em ``bm_run_links`` e a última prova em ``bm_link_state``, sem tabela
de tentativas: as tentativas são um registro curto dentro do próprio resultado.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from buscador_midias.catalog.bright_reader import bright_data_available
from buscador_midias.catalog.health_policy import classify
from buscador_midias.catalog.health_rules import Decision, decide, next_run, recovery_due
from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.catalog import WRITE_LOCK, CatalogRepository
from buscador_midias.db.connection import Repository
from buscador_midias.db.health_actions import (
    activate_recovered,
    apply_decision,
    change_transport,
    restoration_current,
)
from buscador_midias.db.settings import HEALTH_SCOPES, SettingsRepository

KINDS = ("active", "recovery")
OPEN_STATUSES = "('queued','running','cancelling')"
ATTEMPT_LOG_LIMIT = 5
_OPTIONS = {"bright_data", "activate_recovered", "scope"}
_SCOPES = ("errors", "inactive", "auto_excluded_access")


def scope_of(kind: str) -> str:
    """Nome do escopo de configuração de uma rotina (``active`` → ``health_active``)."""
    if kind not in KINDS:
        raise CatalogError("Rotina desconhecida.")
    return f"health_{kind}"


def _attempt_entry(attempt: int, category: str, result: dict[str, Any]) -> dict[str, Any]:
    """Resumo curto de uma tentativa; o resultado completo guarda só a última."""
    return dict(
        attempt=attempt,
        category=category,
        observed_at=result.get("observed_at") or datetime.now(UTC).isoformat(),
        outcome=result.get("outcome"),
        status=result.get("endpoint_status"),
        bright_data_requests=result.get("bright_data_requests", 0),
    )


class HealthRepository(Repository):
    """Cria, consulta e conduz as rodadas de saúde sobre as tabelas ``bm_*``."""

    def install(self) -> None:
        """Cadastro, mecanismos padrão, configurações e tabelas das rodadas."""
        CatalogRepository(self.schema, self.env).install()
        SettingsRepository(self.schema, self.env).install()

    # --- configuração --------------------------------------------------------------------------

    def settings(self) -> list[dict[str, Any]]:
        """Rotina, frequência e próxima execução, no formato que a tela já consome."""
        rows = SettingsRepository(self.schema, self.env).all()
        return [
            dict(
                kind=row["scope"].removeprefix("health_"),
                **row["config"],
                version=row["version"],
                next_run_at=row["next_run_at"],
            )
            for row in rows
            if row["scope"] in HEALTH_SCOPES
        ]

    def configure(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        """Valida revisão, agenda e fallback (delegado às configurações)."""
        saved = SettingsRepository(self.schema, self.env).configure_health(scope_of(kind), data)
        return dict(
            kind=kind, **saved["config"], version=saved["version"], next_run_at=saved["next_run_at"]
        )

    # --- criação -------------------------------------------------------------------------------

    def create(
        self, kind: str, trigger: str = "manual", *, options: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Congela todos os elegíveis; não expande a fila com novas descobertas."""
        scope = scope_of(kind)
        options = options or {}
        if (
            set(options) - _OPTIONS
            or any(type(value) is not bool for key, value in options.items() if key != "scope")
            or options.get("scope", "errors") not in _SCOPES
        ):
            raise CatalogError("Opções da rodada inválidas.")
        if options.get("scope") in ("inactive", "auto_excluded_access") and (
            kind != "recovery" or trigger != "manual"
        ):
            raise CatalogError("A varredura completa de inativos deve ser manual.")
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            setting = db.execute(
                "SELECT * FROM bm_settings WHERE scope=%s FOR UPDATE", (scope,)
            ).fetchone()
            if setting is None:
                raise CatalogError("Configuração não instalada.", 503)
            config = setting["value"]
            if trigger == "scheduled" and (
                not config["enabled"] or setting["next_run_at"] > datetime.now(UTC)
            ):
                return None
            if db.execute(
                "SELECT 1 FROM bm_runs WHERE type='health' AND kind=%s "
                f"AND status IN {OPEN_STATUSES}",
                (kind,),
            ).fetchone():
                if trigger == "scheduled":
                    return None
                raise CatalogError("Já existe uma rodada aberta para esta rotina.", 409)
            configuration = {**config, **options}
            if configuration["bright_data"] and not bright_data_available():
                raise CatalogError("Credenciais Bright Data não configuradas no servidor.")
            models = {r["model"]: r for r in db.execute("SELECT * FROM bm_mechanisms")}
            rows = self._eligible(db, kind, trigger, configuration, models)
            ident = uuid.uuid4().hex
            used = {row["model"] for row in rows}
            configuration["mechanisms"] = {
                model: dict(
                    version=models[model]["version"],
                    parameters=self._health_parameters(models[model], configuration),
                )
                for model in used
            }
            db.execute(
                "INSERT INTO bm_runs(id,type,kind,trigger,status,config) "
                "VALUES(%s,'health',%s,%s,'queued',%s)",
                (ident, kind, trigger, Jsonb(configuration)),
            )
            with db.cursor().copy(
                "COPY bm_run_links(run_id,link_id,site_id,host,link_version,site_version,"
                "mechanism_version) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(
                        (
                            ident,
                            row["id"],
                            row["site_id"],
                            urlsplit(row["site_url"]).hostname or "",
                            row["version"],
                            row["site_version"],
                            models[row["model"]]["version"],
                        )
                    )
            run = db.execute(
                "UPDATE bm_runs SET total=%s WHERE id=%s RETURNING *", (len(rows), ident)
            ).fetchone()
            if trigger == "scheduled":
                db.execute(
                    "UPDATE bm_settings SET next_run_at=%s WHERE scope=%s",
                    (
                        next_run(
                            datetime.now(UTC), config["schedule_hours"], config["interval_minutes"]
                        ),
                        scope,
                    ),
                )
            assert run
            return dict(run)

    @staticmethod
    def _health_parameters(model: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        """Parâmetros do teste de saúde: amostra de texto sem janela de recência."""
        parameters = dict(
            model["parameters"],
            lookback_days=0,
            article_samples=max(1, model["parameters"].get("article_samples", 3)),
        )
        if configuration["bright_data"]:
            parameters["timeout_seconds"] = 60
        return parameters

    def _eligible(
        self,
        db: psycopg.Connection[dict[str, Any]],
        kind: str,
        trigger: str,
        configuration: dict[str, Any],
        models: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Links que a rotina deve testar, pelas mesmas regras da rotina anterior."""
        restore = configuration.get("scope") == "auto_excluded_access"
        condition = (
            "l.active=1 AND s.active=1" if kind == "active" else "(l.active=0 OR s.active=0)"
        )
        last_test = "COALESCE(st.last_test,'{}'::jsonb)" if kind == "recovery" else "'{}'::jsonb"
        rows = db.execute(
            "SELECT l.id,l.site_id,l.url,l.kind,l.mechanism model,l.version,l.deactivated_by,"
            f"l.deactivated_at,{last_test} last_test,s.version site_version,s.active site_active,"
            "l.access_transport,s.url site_url,s.deactivated_by site_deactivated_by,"
            "st.health_category,st.health_checked_at,st.health_config FROM bm_links l "
            "JOIN bm_sites s ON s.id=l.site_id "
            "LEFT JOIN bm_link_state st ON st.link_id=l.id WHERE "
            + ("l.deleted_at IS NOT NULL " if restore else "l.deleted_at IS NULL ")
            + f"AND s.deleted_at IS NULL AND {condition} ORDER BY l.id"
        ).fetchall()
        now = datetime.now(UTC)
        if kind == "recovery" and trigger == "scheduled" and not restore:
            # A rotina agendada de inativos só retesta toda rodada quem a saúde inativou há pouco;
            # os demais voltam a cada SWEEP_DAYS. A execução manual testa todo o escopo.
            rows = [
                row
                for row in rows
                if recovery_due(
                    row["deactivated_at"] if row["deactivated_by"] == "health" else None,
                    row["health_checked_at"],
                    now,
                )
            ]
        if restore:
            return [
                row
                for row in rows
                if row["deactivated_by"] == "validation"
                and classify(row["last_test"]) == "access_error"
                and (row["site_active"] or row["site_deactivated_by"] == "validation")
            ]
        if kind == "recovery" and configuration.get("scope", "errors") == "errors":
            return [row for row in rows if self._access_error(row, models)]
        return rows

    @staticmethod
    def _access_error(row: dict[str, Any], models: dict[str, dict[str, Any]]) -> bool:
        """Só retesta quem falhou por acesso, pela saúde ou pelo último teste manual."""
        previous = row["last_test"]
        version = models[row["model"]]["version"]
        tested = row["health_checked_at"] is not None and row["health_config"] == dict(
            url=row["url"],
            kind=row["kind"],
            model=row["model"],
            model_version=version,
            source_url=row["site_url"],
            access_transport=row["access_transport"],
        )
        if (
            tested
            and previous.get("observed_at")
            and datetime.fromisoformat(previous["observed_at"]) > row["health_checked_at"]
        ):
            tested = False
        manual_current = (
            previous.get("url") == row["url"]
            and previous.get("source_url") == row["site_url"]
            and previous.get("model") == row["model"]
            and previous.get("model_version") == version
        )
        category = (
            row["health_category"] if tested else classify(previous) if manual_current else None
        )
        return category == "access_error"

    # --- consulta ------------------------------------------------------------------------------

    def overview(self, offset: int = 0) -> dict[str, Any]:
        """Histórico paginado e contagem das decisões de cada rodada."""
        with self.connection() as db:
            runs = db.execute(
                "SELECT r.id,r.kind,r.trigger,r.status,r.created_at,r.started_at,r.finished_at,"
                "r.heartbeat_at,r.owner,r.config configuration,r.total,"
                "COALESCE(c.counts,'{}'::jsonb) counts,COALESCE(paid.n,0) bright_data_requests,"
                "COALESCE(act.n,0) activated FROM bm_runs r "
                "LEFT JOIN LATERAL (SELECT jsonb_object_agg(k,n) counts FROM (SELECT "
                "(CASE WHEN state='completed' THEN category ELSE state END) k,count(*) n FROM "
                "bm_run_links WHERE run_id=r.id GROUP BY 1) x) c ON true "
                # Só uma rodada com Bright Data habilitado pode ter gasto.
                "LEFT JOIN LATERAL (SELECT sum((SELECT COALESCE(sum(COALESCE("
                "(e->>'bright_data_requests')::int,0)),0) FROM jsonb_array_elements("
                "COALESCE(rl.result->'attempt_log','[]'::jsonb)) e)) n FROM bm_run_links rl "
                "WHERE rl.run_id=r.id AND r.config->>'bright_data'='true') paid ON true "
                "LEFT JOIN LATERAL (SELECT count(*) n FROM bm_run_links WHERE run_id=r.id "
                "AND activated) act ON true WHERE r.type='health' "
                "ORDER BY r.created_at DESC LIMIT 20 OFFSET %s",
                (offset,),
            ).fetchall()
            count = db.execute("SELECT count(*) n FROM bm_runs WHERE type='health'").fetchone()
            worker = db.execute(
                "SELECT heartbeat_at FROM bm_runs WHERE type='health' AND status='running' "
                "ORDER BY heartbeat_at DESC LIMIT 1"
            ).fetchone()
        assert count
        return dict(
            settings=self.settings(), runs=runs, total=count["n"], offset=offset, worker=worker
        )

    def details(self, ident: str, offset: int = 0, category: str = "") -> dict[str, Any]:
        """Resultados por link com filtro; as tentativas vêm do registro curto do resultado."""
        with self.connection() as db:
            run = db.execute(
                "SELECT id,kind,trigger,status,created_at,started_at,finished_at,heartbeat_at,"
                "owner,config configuration,total FROM bm_runs WHERE id=%s AND type='health'",
                (ident,),
            ).fetchone()
            if not run:
                raise CatalogError("Rodada não encontrada.", 404)
            where = "rl.run_id=%s"
            args: tuple[Any, ...] = (ident,)
            if category == "activated":
                where += " AND rl.activated"
            elif category:
                where += (
                    " AND (CASE WHEN rl.state='completed' THEN rl.category ELSE rl.state END)=%s"
                )
                args = (ident, category)
            count = db.execute(
                f"SELECT count(*) n FROM bm_run_links rl WHERE {where}",
                args,  # noqa: S608
            ).fetchone()
            items = db.execute(
                "SELECT rl.run_id,rl.link_id,rl.site_id source_id,rl.host,rl.state,rl.category,"
                "rl.attempts,rl.result,rl.detail,rl.activated,rl.started_at,rl.finished_at,"
                "rl.link_version,rl.site_version,l.url,l.kind,l.mechanism model,s.name source_name,"
                f"s.url source_url FROM bm_run_links rl JOIN bm_links l ON l.id=rl.link_id "  # noqa: S608
                f"JOIN bm_sites s ON s.id=rl.site_id WHERE {where} "
                "ORDER BY rl.link_id LIMIT 25 OFFSET %s",
                (*args, offset),
            ).fetchall()
        assert count
        for item in items:
            item["history"] = self._history(item)
            item["snapshot"] = dict(
                source=dict(id=item["source_id"], name=item["source_name"], url=item["source_url"]),
                config=dict(url=item["url"], kind=item["kind"], model=item["model"]),
            )
        return dict(run=run, items=items, total=count["n"], offset=offset)

    @staticmethod
    def _history(item: dict[str, Any]) -> list[dict[str, Any]]:
        """Tentativas do link; só a última guarda o resultado completo (as outras, o resumo)."""
        result = item["result"] or {}
        log = [dict(entry) for entry in result.get("attempt_log", [])]
        if log:
            log[-1]["result"] = {k: v for k, v in result.items() if k != "attempt_log"}
        return log

    def cancel(self, ident: str) -> dict[str, Any]:
        """Interrompe o restante da fila; testes em curso encerram dentro de seu orçamento."""
        with self.connection() as db:
            run = db.execute(
                "UPDATE bm_runs SET status='cancelling' WHERE id=%s AND type='health' "
                "AND status IN ('queued','running') RETURNING *",
                (ident,),
            ).fetchone()
        if not run:
            raise CatalogError("Rodada já encerrada ou inexistente.", 409)
        return dict(run)

    # --- condução pelo worker ------------------------------------------------------------------

    def recover(self, owner: str) -> None:
        """Somente o novo líder retoma itens cujo processo anterior foi interrompido."""
        with self.connection() as db:
            db.execute(
                "UPDATE bm_runs SET owner=%s WHERE type='health' "
                "AND status IN ('running','cancelling')",
                (owner,),
            )
            db.execute(
                "UPDATE bm_run_links q SET state='pending',detail='Retomado após interrupção',"
                "attempts=jsonb_array_length(COALESCE(q.result->'attempt_log','[]'::jsonb)) "
                "WHERE q.state='running' AND EXISTS(SELECT 1 FROM bm_runs r WHERE r.id=q.run_id "
                "AND r.type='health' AND r.status IN ('running','cancelling'))"
            )

    def next_run(self, owner: str) -> dict[str, Any] | None:
        """Prioriza rodadas já iniciadas antes das novas e assume a escolhida."""
        with self.connection() as db:
            run = db.execute(
                "SELECT * FROM bm_runs WHERE type='health' "
                "AND status IN ('running','cancelling','queued') "
                "ORDER BY (status='queued'),created_at LIMIT 1 FOR UPDATE"
            ).fetchone()
            if not run:
                return None
            updated = db.execute(
                "UPDATE bm_runs SET owner=%s,status=CASE WHEN status='queued' THEN 'running' "
                "ELSE status END,started_at=COALESCE(started_at,now()),heartbeat_at=now() "
                "WHERE id=%s RETURNING *",
                (owner, run["id"]),
            ).fetchone()
        return dict(updated) if updated else None

    def big_hosts(self, run_id: str, threshold: int) -> set[str]:
        """Sites com muitos links na rodada: aceitam algumas leituras ao mesmo tempo."""
        with self.connection() as db:
            return {
                r["host"]
                for r in db.execute(
                    "SELECT host FROM bm_run_links WHERE run_id=%s GROUP BY host "
                    "HAVING count(*)>%s",
                    (run_id, threshold),
                )
            }

    def reserve(
        self,
        run: dict[str, Any],
        owner: str,
        busy: list[str],
        free_slots: int,
        big_hosts: set[str],
        big_slots: int,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Batimento, cancelamento e reserva de itens (um por host, vários nos sites grandes).

        Devolve ``(status, itens)``; ``status`` é ``None`` se a rodada deixou de ser deste dono.
        """
        with self.connection() as db:
            state = db.execute(
                "UPDATE bm_runs SET heartbeat_at=now() WHERE id=%s AND owner=%s RETURNING status",
                (run["id"], owner),
            ).fetchone()
            if not state:
                return None, []
            if state["status"] == "cancelling":
                db.execute(
                    "UPDATE bm_run_links SET state='cancelled',finished_at=now() "
                    "WHERE run_id=%s AND state='pending'",
                    (run["id"],),
                )
                return state["status"], []
            reserved: list[dict[str, Any]] = []
            for _ in range(free_slots):
                counts = Counter([*busy, *(r["host"] for r in reserved)])
                full = [
                    host
                    for host, count in counts.items()
                    if count >= (big_slots if host in big_hosts else 1)
                ]
                row = db.execute(
                    "SELECT * FROM bm_run_links WHERE run_id=%s AND state='pending' "
                    "AND host<>ALL(%s) ORDER BY link_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (run["id"], full),
                ).fetchone()
                if not row:
                    break
                db.execute(
                    "UPDATE bm_run_links SET state='running',started_at=now() "
                    "WHERE run_id=%s AND link_id=%s",
                    (run["id"], row["link_id"]),
                )
                reserved.append(row)
            return state["status"], self._items(db, run, reserved)

    @staticmethod
    def _items(
        db: psycopg.Connection[dict[str, Any]], run: dict[str, Any], rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """O que o teste precisa de cada item: revisões congeladas e o cadastro de agora."""
        if not rows:
            return []
        links = {
            r["id"]: r
            for r in db.execute(
                "SELECT l.id,l.url,l.kind,l.mechanism,l.access_transport,s.name,s.url site_url "
                "FROM bm_links l JOIN bm_sites s ON s.id=l.site_id WHERE l.id=ANY(%s)",
                ([r["link_id"] for r in rows],),
            )
        }
        config = run["config"]
        items = []
        for row in rows:
            link = links[row["link_id"]]
            snapshot: dict[str, Any] = dict(
                link_id=row["link_id"],
                link_version=row["link_version"],
                source_version=row["site_version"],
                source=dict(id=row["site_id"], name=link["name"], url=link["site_url"]),
                config=dict(
                    bright_data=config["bright_data"],
                    access_transport=link["access_transport"],
                    max_bright_requests=config["max_bright_requests"],
                    url=link["url"],
                    kind=link["kind"],
                    model=link["mechanism"],
                    model_version=row["mechanism_version"],
                    parameters=config["mechanisms"][link["mechanism"]]["parameters"],
                ),
            )
            if config.get("scope") == "auto_excluded_access":
                snapshot["restore_run_id"] = run["id"]
            items.append(dict(row, snapshot=snapshot))
        return items

    def run_state(self, run_id: str) -> tuple[str, str | None] | None:
        """Situação e dono atuais da rodada."""
        with self.connection() as db:
            row = db.execute("SELECT status,owner FROM bm_runs WHERE id=%s", (run_id,)).fetchone()
        return (row["status"], row["owner"]) if row else None

    def is_current(self, snapshot: dict[str, Any], kind: str) -> bool:
        """O cadastro continua nas revisões testadas e no estado que a rotina espera."""
        with self.connection() as db:
            if snapshot.get("restore_run_id"):
                return restoration_current(db, snapshot)
            row = db.execute(
                "SELECT l.version,l.active,l.deleted_at,s.version sv,s.active sa,s.deleted_at sd,"
                "m.version mv FROM bm_links l JOIN bm_sites s ON s.id=l.site_id "
                "JOIN bm_mechanisms m ON m.model=l.mechanism WHERE l.id=%s",
                (snapshot["link_id"],),
            ).fetchone()
        if not row or row["deleted_at"] or row["sd"]:
            return False
        effective = bool(row["active"] and row["sa"])
        return bool(
            (row["version"], row["sv"], row["mv"])
            == (
                snapshot["link_version"],
                snapshot["source_version"],
                snapshot["config"]["model_version"],
            )
            and (effective if kind == "active" else not effective)
        )

    def finish(
        self,
        run: dict[str, Any],
        row: dict[str, Any],
        owner: str,
        state: str,
        observation: tuple[int, str, dict[str, Any]] | None,
        detail: str,
        *,
        gate: str | None = None,
    ) -> None:
        """Grava a tentativa e, se final, o resultado, a ativação e a última verificação."""
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            if not self._owns(db, run["id"], owner):
                return
            snapshot = row["snapshot"]
            if state == "completed" and not self._unchanged(db, snapshot):
                state, detail = "skipped", "Cadastro alterado durante o teste"
            category = observation[1] if observation else None
            result = self._with_log(db, run["id"], row["link_id"], observation)
            db.execute(
                "UPDATE bm_run_links SET state=%s,category=%s,result=COALESCE(%s,result),"
                "detail=%s,attempts=COALESCE(%s,attempts),"
                "finished_at=CASE WHEN %s='running' THEN NULL ELSE now() END "
                "WHERE run_id=%s AND link_id=%s",
                (
                    state,
                    category,
                    Jsonb(result) if result is not None else None,
                    detail,
                    observation[0] if observation else None,
                    state,
                    run["id"],
                    row["link_id"],
                ),
            )
            if state != "completed" or not category:
                return
            if (
                category == "healthy"
                and run["config"]["activate_recovered"]
                and run["kind"] == "recovery"
            ):
                self._reactivate(db, run, row, observation, gate)
            self._record_check(db, run["id"], snapshot, category)

    def finish_probe(
        self, run: dict[str, Any], row: dict[str, Any], owner: str, outcome: dict[str, Any]
    ) -> None:
        """Grava o resultado da sonda e aplica a decisão na mesma transação, na revisão testada."""
        snapshot = row["snapshot"]
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            if not self._owns(db, run["id"], owner):
                return
            if not self._unchanged(db, snapshot):
                db.execute(
                    "UPDATE bm_run_links SET state='skipped',detail=%s,finished_at=now() "
                    "WHERE run_id=%s AND link_id=%s",
                    ("Cadastro alterado durante o teste", run["id"], row["link_id"]),
                )
                return
            previous = db.execute(
                "SELECT health_failures FROM bm_link_state WHERE link_id=%s", (row["link_id"],)
            ).fetchone()
            decision = decide(outcome, previous["health_failures"] if previous else 0)
            versions = self._apply(db, run["id"], snapshot, decision, outcome)
            result = self._with_log(db, run["id"], row["link_id"], (1, decision.category, outcome))
            db.execute(
                "UPDATE bm_run_links SET state='completed',category=%s,result=%s,detail=%s,"
                "attempts=1,finished_at=now() WHERE run_id=%s AND link_id=%s",
                (decision.category, Jsonb(result), decision.reason, run["id"], row["link_id"]),
            )
            self._record_check(
                db,
                run["id"],
                dict(snapshot, link_version=versions),
                decision.category,
                failures=decision.failures,
            )

    def complete_run(self, run: dict[str, Any], owner: str) -> None:
        """Fecha a rodada (cancelada se pedido), resume as decisões e agenda a próxima."""
        with self.connection() as db:
            closed = db.execute(
                "UPDATE bm_runs SET status=CASE WHEN status='cancelling' THEN 'cancelled' "
                "ELSE 'completed' END,finished_at=now(),heartbeat_at=now(),"
                "stats=jsonb_build_object('counts',COALESCE((SELECT jsonb_object_agg(k,n) FROM "
                "(SELECT (CASE WHEN state='completed' THEN category ELSE state END) k,count(*) n "
                "FROM bm_run_links WHERE run_id=%(id)s GROUP BY 1) x),'{}'::jsonb),'activated',"
                "(SELECT count(*) FROM bm_run_links WHERE run_id=%(id)s AND activated)),"
                "stats_final=true WHERE id=%(id)s AND owner=%(owner)s RETURNING id",
                dict(id=run["id"], owner=owner),
            ).fetchone()
            if not closed:
                return
            setting = db.execute(
                "SELECT value FROM bm_settings WHERE scope=%s", (scope_of(run["kind"]),)
            ).fetchone()
            assert setting
            config = setting["value"]
            db.execute(
                "UPDATE bm_settings SET next_run_at=GREATEST(next_run_at,%s) WHERE scope=%s",
                (
                    next_run(
                        datetime.now(UTC), config["schedule_hours"], config["interval_minutes"]
                    ),
                    scope_of(run["kind"]),
                ),
            )

    # --- internos ------------------------------------------------------------------------------

    @staticmethod
    def _owns(db: psycopg.Connection[dict[str, Any]], run_id: str, owner: str) -> bool:
        row = db.execute("SELECT owner FROM bm_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
        return bool(row and row["owner"] == owner)

    @staticmethod
    def _unchanged(db: psycopg.Connection[dict[str, Any]], snapshot: dict[str, Any]) -> bool:
        if snapshot.get("restore_run_id"):
            return restoration_current(db, snapshot)
        current = db.execute(
            "SELECT l.version,s.version sv,m.version mv FROM bm_links l "
            "JOIN bm_sites s ON s.id=l.site_id JOIN bm_mechanisms m ON m.model=l.mechanism "
            "WHERE l.id=%s AND l.deleted_at IS NULL AND s.deleted_at IS NULL",
            (snapshot["link_id"],),
        ).fetchone()
        return bool(
            current
            and (current["version"], current["sv"], current["mv"])
            == (
                snapshot["link_version"],
                snapshot["source_version"],
                snapshot["config"]["model_version"],
            )
        )

    @staticmethod
    def _with_log(
        db: psycopg.Connection[dict[str, Any]],
        run_id: str,
        link_id: int,
        observation: tuple[int, str, dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """Resultado completo da última tentativa mais o registro curto das anteriores."""
        if observation is None:
            return None
        attempt, category, result = observation
        stored = db.execute(
            "SELECT result FROM bm_run_links WHERE run_id=%s AND link_id=%s", (run_id, link_id)
        ).fetchone()
        log = ((stored or {}).get("result") or {}).get("attempt_log", [])
        log = [e for e in log if e.get("attempt") != attempt]
        log.append(_attempt_entry(attempt, category, result))
        return {**result, "attempt_log": log[-ATTEMPT_LOG_LIMIT:]}

    @staticmethod
    def _apply(
        db: psycopg.Connection[dict[str, Any]],
        run_id: str,
        snapshot: dict[str, Any],
        decision: Decision,
        outcome: dict[str, Any],
    ) -> int:
        version = apply_decision(db, snapshot, run_id, decision) or snapshot["link_version"]
        if outcome.get("transport_changed") and decision.action in ("keep", "review"):
            version = (
                change_transport(
                    db, dict(snapshot, link_version=version), run_id, outcome["transport"]
                )
                or version
            )
        return int(version)

    @staticmethod
    def _reactivate(
        db: psycopg.Connection[dict[str, Any]],
        run: dict[str, Any],
        row: dict[str, Any],
        observation: tuple[int, str, dict[str, Any]] | None,
        gate: str | None,
    ) -> None:
        if gate:
            db.execute(
                "UPDATE bm_run_links SET detail=%s WHERE run_id=%s AND link_id=%s",
                (gate, run["id"], row["link_id"]),
            )
        elif activate_recovered(db, row["snapshot"], observation[2] if observation else {}):
            db.execute(
                "UPDATE bm_run_links SET activated=true WHERE run_id=%s AND link_id=%s",
                (run["id"], row["link_id"]),
            )

    @staticmethod
    def _record_check(
        db: psycopg.Connection[dict[str, Any]],
        run_id: str,
        snapshot: dict[str, Any],
        category: str,
        failures: int | None = None,
    ) -> None:
        """Última verificação do link e a configuração que ela provou (a que está no cadastro)."""
        db.execute(
            "INSERT INTO bm_link_state(link_id,health_run_id,health_category,health_checked_at,"
            "health_config,health_failures) SELECT l.id,%(run)s,%(category)s,now(),"
            "jsonb_build_object('url',l.url,'kind',l.kind,'model',l.mechanism,"
            "'model_version',m.version,'source_url',s.url,'access_transport',l.access_transport),"
            "COALESCE(%(failures)s,0) FROM bm_links l JOIN bm_sites s ON s.id=l.site_id "
            "JOIN bm_mechanisms m ON m.model=l.mechanism WHERE l.id=%(link)s "
            "ON CONFLICT (link_id) DO UPDATE SET health_run_id=EXCLUDED.health_run_id,"
            "health_category=EXCLUDED.health_category,health_checked_at=now(),"
            "health_config=EXCLUDED.health_config,"
            "health_failures=COALESCE(%(failures)s,bm_link_state.health_failures),"
            "updated_at=now()",
            dict(run=run_id, category=category, failures=failures, link=snapshot["link_id"]),
        )
