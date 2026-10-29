"""Limpeza periódica: o que já está no corpus ou perdeu a utilidade sai de ``bm_*``.

Cada categoria tem a sua idade e roda sozinha (um timer do systemd por categoria). A linha de uma
notícia concluída só diz "esta URL já foi vista"; quando é gravada ou já conhecida, o corpus
passa a dizer isso por ``url_hash`` e a linha pode sair. As descartadas guardam a memória que
evita ler a mesma página de novo, por isso ficam mais tempo. Falhas, trabalho em andamento e
``evidence_pending`` nunca são apagados aqui: esperam decisão ou ainda têm o que fazer.
Cada execução fica registrada em ``bm_cleanup_runs`` para a tela acompanhar.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.connection import Repository

BATCH = 2000
MAX_BATCH = 20000
#: Pausa entre lotes: o Postgres é compartilhado com o PowerDD.
PAUSE_SECONDS = 0.2
#: Fuso dos horários dos timers (``OnCalendar`` do systemd usa o relógio local da máquina).
ZONE = ZoneInfo("America/Sao_Paulo")
#: Uma execução sem fim há mais que isto foi interrompida (processo morto, máquina desligada).
STALE = timedelta(hours=1)
HISTORY_DAYS = 90
SHOWN_RUNS = 5
_DISCARDED = "('outside_window','date_unknown','no_text','excluded_content','no_entities')"
_SETTLED = (
    "state='done' AND updated_at < now()-make_interval(days=>%(days)s::int) "
    "AND last_seen_at < now()-make_interval(days=>%(days)s::int)"
)


@dataclass(frozen=True)
class Category:
    """Uma categoria de limpeza: idade padrão, horário do timer e o que ela faz com a tabela."""

    days: int
    at: str
    description: str
    where: str
    change: str = "DELETE FROM bm_articles"
    table: str = "bm_articles"
    key: str = "hash"

    @property
    def sql(self) -> str:
        """Um lote que não espera por linhas em uso: o que estiver travado fica para a próxima."""
        return (
            f"{self.change} WHERE {self.key} IN (SELECT {self.key} FROM {self.table} "  # noqa: S608
            f"WHERE {self.where} ORDER BY {self.key} LIMIT %(batch)s FOR UPDATE SKIP LOCKED)"
        )

    @property
    def count_sql(self) -> str:
        """Quantas linhas a categoria limparia agora."""
        return f"SELECT count(*) n FROM {self.table} WHERE {self.where}"  # noqa: S608


CATEGORIES = {
    "concluidas": Category(
        3,
        "03:30",
        "Notícias gravadas ou já conhecidas: o corpus guarda o resultado.",
        f"{_SETTLED} AND outcome IN ('persisted','known')",
    ),
    "descartadas": Category(
        14,
        "03:40",
        "Notícias descartadas (fora da janela, sem data, sem texto, conteúdo excluído, sem entidade).",
        f"{_SETTLED} AND outcome IN {_DISCARDED}",
    ),
    "corpo-descartadas": Category(
        1,
        "03:50",
        "Texto da página das descartadas; a análise e os motivos continuam na linha.",
        "state='done' AND updated_at < now()-make_interval(days=>%(days)s::int) "
        f"AND outcome IN {_DISCARDED} AND payload ? 'body'",
        change="UPDATE bm_articles SET payload=payload-'body'",
    ),
}


def next_run(at: str, now: datetime | None = None) -> datetime:
    """A próxima vez que o relógio local marca ``at`` ("HH:MM"), em UTC."""
    moment = (now or datetime.now(UTC)).astimezone(ZONE)
    hour, minute = (int(part) for part in at.split(":"))
    candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= moment:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


class RetentionRepository(Repository):
    """Roda uma categoria de limpeza em lotes pequenos, cada um confirmado à parte."""

    def run(self, category: str, *, days: int | None = None, batch: int = BATCH) -> int:
        """Limpa a categoria; devolve quantas linhas saíram (ou perderam o texto).

        A execução fica em ``bm_cleanup_runs`` desde o início, para a tela mostrar "em andamento",
        e termina com o total ou com o erro (que continua subindo).
        """
        spec = CATEGORIES.get(category)
        if spec is None:
            raise CatalogError("Categoria de limpeza desconhecida: " + ", ".join(CATEGORIES))
        age = spec.days if days is None else days
        if age < 1:
            raise CatalogError("A idade mínima de limpeza é 1 dia.")
        if not 1 <= batch <= MAX_BATCH:
            raise CatalogError(f"O lote deve ficar entre 1 e {MAX_BATCH}.")
        with self.connection() as db:
            started = db.execute(
                "INSERT INTO bm_cleanup_runs(category,days) VALUES(%s,%s) RETURNING id",
                (category, age),
            ).fetchone()
            assert started is not None
            run_id = started["id"]
        total = 0
        try:
            while True:
                with self.connection() as db:
                    done = db.execute(spec.sql, dict(days=age, batch=batch)).rowcount
                total += done
                if done < batch:
                    break
                time.sleep(PAUSE_SECONDS)
        except Exception as error:
            self._finish(run_id, total, type(error).__name__)
            raise
        self._finish(run_id, total, None)
        return total

    def _finish(self, run_id: int, removed: int, error: str | None) -> None:
        """Fecha o registro da execução e apaga o histórico muito antigo da categoria."""
        with self.connection() as db:
            row = db.execute(
                "UPDATE bm_cleanup_runs SET finished_at=now(),removed=%s,error=%s WHERE id=%s "
                "RETURNING category",
                (removed, error, run_id),
            ).fetchone()
            assert row is not None
            db.execute(
                "DELETE FROM bm_cleanup_runs WHERE category=%s "
                "AND started_at < now()-make_interval(days=>%s)",
                (row["category"], HISTORY_DAYS),
            )

    def status(self) -> list[dict[str, Any]]:
        """Para a tela: horário, última execução, próxima e quanto está esperando em cada categoria."""
        now = datetime.now(UTC)
        result = []
        with self.connection() as db:
            for name, spec in CATEGORIES.items():
                runs = db.execute(
                    "SELECT id,started_at,finished_at,days,removed,error FROM bm_cleanup_runs "
                    "WHERE category=%s ORDER BY id DESC LIMIT %s",
                    (name, SHOWN_RUNS),
                ).fetchall()
                for run in runs:
                    run["state"] = self._state(run, now)
                counted = db.execute(spec.count_sql, dict(days=spec.days)).fetchone()
                assert counted is not None
                eligible = counted["n"]
                result.append(
                    dict(
                        category=name,
                        description=spec.description,
                        days=spec.days,
                        at=spec.at,
                        next_run=next_run(spec.at, now),
                        eligible=eligible,
                        last=runs[0] if runs else None,
                        runs=runs,
                    )
                )
        return result

    @staticmethod
    def _state(run: dict[str, Any], now: datetime) -> str:
        if run["error"]:
            return "error"
        if run["finished_at"] is not None:
            return "done"
        return "running" if now - run["started_at"] < STALE else "interrupted"
