"""Configuração única do buscador: regras de validação e a tabela ``bm_settings``.

Um escopo por linha: ``search`` (rodadas de busca), ``health_active`` e ``health_recovery``
(rotinas de saúde). Substitui ``ingest_settings`` e ``health_settings``. A validação é código
puro (testável sem banco) e vale igual para a API, os scripts e o seed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from buscador_midias.catalog.bright_reader import bright_data_available
from buscador_midias.catalog.health_rules import next_run
from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.connection import Repository, connect
from buscador_midias.db.schema import install as install_tables

STAGES = ("discovery", "dedup", "fetch", "context", "evidence", "persist")
FILTER_MODES = ("off", "shadow", "enforce")
# Notícia e opinião nunca são bloqueadas: o restante é rotina sem interesse para o corpus.
BLOCKABLE_CATEGORIES = ("weather", "cooking", "sports", "entertainment", "lifestyle", "horoscope")
MAX_WORKERS = 64

SEARCH_SCOPE = "search"
HEALTH_SCOPES = ("health_active", "health_recovery")

SEARCH_DEFAULTS: dict[str, Any] = dict(
    enabled=False,
    workers_enabled=True,
    window_mode="today",
    timezone="America/Sao_Paulo",
    schedule_mode="clock",
    schedule_hours=[1, 6, 12, 16],
    daily_routes_only=True,
    lookback_minutes=2880,
    allow_undated=False,
    bright_data=False,
    evidence_paid_adverse_only=True,
    max_attempts=3,
    retry_minutes=1,
    model="openai/gpt-4.1-mini",
    analysis_text_chars=12000,
    content_filter="shadow",
    blocked_content_categories=["horoscope", "weather", "cooking", "sports"],
    lastmod_filter="shadow",
    url_filter="shadow",
    head_filter="shadow",
    host_concurrency=1,
    # Teto de vagas por host com fila longa; 1 desliga.
    host_concurrency_max=1,
    # Fração de links lidos por inteiro para auditar o corte pelo topo.
    head_audit_rate=0.0,
    # "enforce": rodadas agendadas pulam links frios; "off" lê todos.
    adaptive_polling="off",
    # A primeira rodada do dia lê também o dia anterior inteiro.
    close_previous_day=False,
    # Passadas totais por notícia que falha por motivo transitório: 1 = nunca tenta de novo.
    failed_retry_limit=1,
    workers=dict(discovery=2, dedup=1, fetch=2, context=1, evidence=1, persist=1),
)

_BOOLEANS = (
    "enabled",
    "workers_enabled",
    "daily_routes_only",
    "allow_undated",
    "bright_data",
    "evidence_paid_adverse_only",
)
_MODES = ("content_filter", "lastmod_filter", "url_filter", "head_filter")
_BOUNDS = {
    "lookback_minutes": (1, 5256000),
    "max_attempts": (1, 3),
    "retry_minutes": (1, 1440),
    "analysis_text_chars": (1000, 30000),
    "host_concurrency": (1, 4),
    "host_concurrency_max": (1, 8),
    "failed_retry_limit": (1, 5),
}


def _hours(value: Any, *, allow_empty: bool) -> list[int]:
    ok = (
        isinstance(value, list)
        and (allow_empty or value)
        and len(value) <= 24
        and all(type(hour) is int and 0 <= hour <= 23 for hour in value)
        and len(set(value)) == len(value)
    )
    if not ok:
        raise CatalogError("Informe horas distintas de 0 a 23.")
    return sorted(value)


def _check(key: str, value: Any) -> Any:
    """Valida uma chave da busca; devolve o valor aceito ou levanta ``CatalogError``."""
    if key in _BOOLEANS and type(value) is not bool:
        raise CatalogError("Informe opções booleanas válidas.")
    if key == "schedule_mode" and value not in ("clock", "link_interval"):
        raise CatalogError("Selecione o modo de agendamento válido.")
    if key == "schedule_hours":
        _hours(value, allow_empty=False)
    if key == "window_mode" and value not in ("today", "lookback"):
        raise CatalogError("Selecione o modo de janela válido.")
    if key == "timezone" and value not in ("America/Sao_Paulo", "UTC"):
        raise CatalogError("Selecione um fuso horário válido.")
    if key == "workers" and (
        not isinstance(value, dict)
        or set(value) != set(STAGES)
        or any(type(v) is not int or not 1 <= v <= MAX_WORKERS for v in value.values())
    ):
        raise CatalogError(f"Informe 1 a {MAX_WORKERS} workers por etapa.")
    if key in _MODES and value not in FILTER_MODES:
        raise CatalogError("Selecione um modo de filtro válido (off, shadow ou enforce).")
    if key == "close_previous_day" and type(value) is not bool:
        raise CatalogError("Informe verdadeiro ou falso para fechar o dia anterior.")
    if key == "adaptive_polling" and value not in ("off", "enforce"):
        raise CatalogError("Selecione o modo da leitura adaptativa válido.")
    if key == "head_audit_rate" and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1
    ):
        raise CatalogError("Informe a fração da auditoria do corte entre 0 e 1.")
    if key == "blocked_content_categories" and (
        not isinstance(value, list)
        or any(v not in BLOCKABLE_CATEGORIES for v in value)
        or len(set(value)) != len(value)
    ):
        raise CatalogError("Informe categorias de conteúdo bloqueáveis e distintas.")
    if key == "model" and (
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,150}", value)
    ):
        raise CatalogError("Modelo inválido.")
    if key in _BOUNDS:
        low, high = _BOUNDS[key]
        if type(value) is not int or not low <= value <= high:
            raise CatalogError(f"{key}: informe um inteiro entre {low} e {high}.")
    return value


def validate_search_patch(patch: Any) -> dict[str, Any]:
    """Valida um conjunto parcial de chaves da busca e o devolve sem alterações."""
    if not isinstance(patch, dict) or patch.keys() - SEARCH_DEFAULTS.keys():
        raise CatalogError("Parâmetros de busca desconhecidos.")
    if patch.get("bright_data") is True:
        raise CatalogError("O fallback Bright Data está desativado na busca de notícias.")
    for key, value in patch.items():
        _check(key, value)
    return patch


def health_defaults(scope: str) -> dict[str, Any]:
    """Valores iniciais de uma rotina de saúde (iguais aos padrões do esquema antigo)."""
    if scope not in HEALTH_SCOPES:
        raise CatalogError("Configuração de saúde inválida.")
    return dict(
        enabled=True,
        interval_minutes=1440,
        workers=8,
        max_attempts=2,
        bright_data=False,
        max_bright_requests=8,
        activate_recovered=False,
        schedule_hours=[],
        mode="full",
        scope="errors",
    )


def validate_health(scope: str, values: dict[str, Any]) -> dict[str, Any]:
    """Valida os valores completos de uma rotina; devolve com os horários ordenados."""
    if scope not in HEALTH_SCOPES or values.keys() != health_defaults(scope).keys():
        raise CatalogError("Configuração de saúde inválida.")
    if any(
        type(values[k]) is not bool for k in ("enabled", "bright_data", "activate_recovered")
    ) or any(
        type(values[k]) is not int or not low <= values[k] <= high
        for k, low, high in (
            ("interval_minutes", 1, 525600),
            ("workers", 1, 32),
            ("max_attempts", 1, 3),
            ("max_bright_requests", 1, 48),
        )
    ):
        raise CatalogError("Configuração inválida: confira frequência, paralelismo e tentativas.")
    hours = _hours(values["schedule_hours"], allow_empty=True)
    if (
        values["mode"] not in ("full", "probe")
        or values["scope"] not in ("errors", "inactive")
        or (scope == "health_recovery" and values["mode"] == "probe")
        or (scope == "health_active" and values["scope"] != "errors")
    ):
        raise CatalogError("Configuração inválida: confira horários, modo e escopo.")
    return {**values, "schedule_hours": hours}


class SettingsRepository(Repository):
    """Lê e grava ``bm_settings`` com revisão otimista e auditoria por diff."""

    def install(self) -> None:
        """Cria as tabelas e os três escopos que faltam, sem alterar o que já foi salvo."""
        with connect(self.schema, env=self.env, create=True) as db:
            install_tables(db)
            db.execute(
                "INSERT INTO bm_settings(scope,value) VALUES(%s,%s) ON CONFLICT (scope) DO NOTHING",
                (SEARCH_SCOPE, Jsonb(SEARCH_DEFAULTS)),
            )
            for scope in HEALTH_SCOPES:
                db.execute(
                    "INSERT INTO bm_settings(scope,value,next_run_at) VALUES(%s,%s,"
                    "now()+interval '24 hours') ON CONFLICT (scope) DO NOTHING",
                    (scope, Jsonb(health_defaults(scope))),
                )

    def all(self) -> list[dict[str, Any]]:
        """Todos os escopos, por nome."""
        with self.connection() as db:
            rows = db.execute("SELECT * FROM bm_settings ORDER BY scope").fetchall()
        return [self._row(r) for r in rows]

    def search(self) -> dict[str, Any]:
        """Configuração atual da busca: ``config`` e ``version``."""
        return self._get(SEARCH_SCOPE)

    def health(self, scope: str) -> dict[str, Any]:
        """Configuração atual de uma rotina de saúde."""
        if scope not in HEALTH_SCOPES:
            raise CatalogError("Configuração de saúde inválida.")
        return self._get(scope)

    def configure_search(self, data: dict[str, Any]) -> dict[str, Any]:
        """Valida revisão, tipos e limites antes de alterar as próximas rodadas."""
        if set(data) - {"version", "config"} or type(data.get("version")) is not int:
            raise CatalogError("Configuração inválida.")
        patch = validate_search_patch(data.get("config"))
        return self._update(SEARCH_SCOPE, data["version"], patch, lambda merged, _: merged)

    def configure_health(self, scope: str, data: dict[str, Any]) -> dict[str, Any]:
        """Valida revisão, agenda e fallback de uma rotina; recalcula a próxima execução."""
        version = data.get("version")
        if scope not in HEALTH_SCOPES or type(version) is not int:
            raise CatalogError("Configuração de saúde inválida.")
        patch = {k: v for k, v in data.items() if k != "version"}
        if patch.keys() - health_defaults(scope).keys():
            raise CatalogError("Configuração de saúde inválida.")

        def check(merged: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
            values = validate_health(scope, merged)
            if values["bright_data"] and not bright_data_available():
                raise CatalogError("Credenciais Bright Data não configuradas no servidor.")
            return values

        return self._update(scope, version, patch, check, schedule=True)

    # --- internos ------------------------------------------------------------------------------

    @staticmethod
    def _row(row: dict[str, Any]) -> dict[str, Any]:
        return dict(
            scope=row["scope"],
            config=row["value"],
            version=row["version"],
            next_run_at=row["next_run_at"],
            updated_at=row["updated_at"],
        )

    def _get(self, scope: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM bm_settings WHERE scope=%s", (scope,)).fetchone()
        if row is None:
            raise CatalogError("Configuração não instalada.", 503)
        return self._row(row)

    def _update(
        self,
        scope: str,
        version: int,
        patch: dict[str, Any],
        check: Any,
        *,
        schedule: bool = False,
    ) -> dict[str, Any]:
        with self.connection() as db:
            old = db.execute(
                "SELECT * FROM bm_settings WHERE scope=%s FOR UPDATE", (scope,)
            ).fetchone()
            if old is None:
                raise CatalogError("Configuração não instalada.", 503)
            if old["version"] != version:
                raise CatalogError("A configuração mudou. Recarregue a tela.", 409)
            merged = check({**old["value"], **patch}, old["value"])
            next_at = old["next_run_at"]
            if schedule and (
                old["value"]["interval_minutes"] != merged["interval_minutes"]
                or old["value"]["enabled"] != merged["enabled"]
                or old["value"]["schedule_hours"] != merged["schedule_hours"]
            ):
                next_at = next_run(
                    datetime.now(UTC), merged["schedule_hours"], merged["interval_minutes"]
                )
            row = db.execute(
                "UPDATE bm_settings SET value=%s,version=version+1,next_run_at=%s,"
                "updated_at=now() WHERE scope=%s RETURNING *",
                (Jsonb(merged), next_at, scope),
            ).fetchone()
            assert row
            changes = {
                key: [old["value"].get(key), value]
                for key, value in merged.items()
                if old["value"].get(key) != value
            }
            db.execute(
                "INSERT INTO bm_events(entity,action,actor,changes) VALUES('settings','update',"
                "'user',%s)",
                (Jsonb({"scope": scope, **changes}),),
            )
            return self._row(row)
