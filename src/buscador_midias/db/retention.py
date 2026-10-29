"""Limpeza periódica: o que já está no corpus ou perdeu a utilidade sai de ``bm_*``.

Cada categoria roda sozinha (um timer do systemd por categoria), em horário fixo ou de tempos em
tempos. A categoria ``conferencia-corpus`` não limpa por idade: fecha as notícias em aberto cujo
artigo o corpus já tem (a conferência do início da fila, repetida antes de gastar IA). A linha de
uma notícia concluída só diz "esta URL já foi vista"; quando é gravada ou já conhecida, o corpus
passa a dizer isso por ``url_hash`` e a linha pode sair. As descartadas guardam a memória que
evita ler a mesma página de novo, por isso ficam mais tempo. Falhas, trabalho em andamento e
``evidence_pending`` nunca são apagados por idade: esperam decisão ou ainda têm o que fazer.
Cada execução fica registrada em ``bm_cleanup_runs`` para a tela acompanhar.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import sql

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.connection import Repository
from buscador_midias.db.queue import PURGED_ON_DONE

BATCH = 2000
MAX_BATCH = 20000
#: Pausa entre lotes: o Postgres é compartilhado com o PowerDD.
PAUSE_SECONDS = 0.2
#: Fuso dos horários dos timers (``OnCalendar`` do systemd usa o relógio local da máquina).
ZONE = ZoneInfo("America/Sao_Paulo")
#: Uma execução sem fim há mais que isto foi interrompida (processo morto, máquina desligada).
STALE = timedelta(hours=1)
HISTORY_DAYS = 90
#: Teto de execuções guardadas por categoria (as frequentes encheriam a tabela em dias).
HISTORY_RUNS = 200
SHOWN_RUNS = 5
_DISCARDED = "('outside_window','date_unknown','no_text','excluded_content','no_entities')"
_SETTLED = (
    "state='done' AND updated_at < now()-make_interval(days=>%(days)s::int) "
    "AND last_seen_at < now()-make_interval(days=>%(days)s::int)"
)
#: Notícia em aberto (ou falha) que o corpus já tem: nada a analisar. ``running`` fica de fora,
#: porque um worker já a tem em mãos e ele mesmo a conclui.
_CLOSE_AS_KNOWN = (
    "UPDATE bm_articles SET state='done',outcome='known',attempts=0,owner=NULL,"
    "locked_until=NULL,queued_at=NULL,failed_stage=NULL,reason=NULL,reason_class=NULL,"
    "retryable=NULL,failed_at=NULL,detail=NULL,updated_at=now(),"
    "payload=payload" + "".join(f"-'{key}'" for key in PURGED_ON_DONE) + ","
    "corpus_article_id=(SELECT c.id FROM {corpus} c WHERE c.url_hash=bm_articles.hash)"
)


@dataclass(frozen=True)
class Category:
    """Uma categoria de limpeza: quando roda, de que idade e o que faz com a tabela.

    ``at`` ("HH:MM") roda uma vez por dia; ``every`` (minutos) roda de tempos em tempos. ``days``
    é a idade mínima e fica ``None`` quando a categoria não olha idade. O SQL pode citar a tabela
    de artigos do corpus como ``{corpus}``.
    """

    description: str
    where: str
    days: int | None = None
    at: str | None = None
    every: int | None = None
    change: str = "DELETE FROM bm_articles"
    table: str = "bm_articles"
    key: str = "hash"

    @property
    def schedule(self) -> str:
        """O horário como a tela o mostra."""
        return self.at if self.at else f"a cada {self.every} min"

    @property
    def sql(self) -> str:
        """Um lote que não espera por linhas em uso: o que estiver travado fica para a próxima."""
        return (
            f"{self.change} WHERE {self.key} IN (SELECT {self.key} FROM {self.table} "  # noqa: S608
            f"WHERE {self.where} ORDER BY {self.key} LIMIT %(batch)s FOR UPDATE SKIP LOCKED)"
        )

    @property
    def count_sql(self) -> str:
        """Quantas linhas a categoria trataria agora."""
        return f"SELECT count(*) n FROM {self.table} WHERE {self.where}"  # noqa: S608

    def next_run(self, now: datetime | None = None) -> datetime:
        """A próxima vez que o timer da categoria dispara, em UTC."""
        moment = (now or datetime.now(UTC)).astimezone(ZONE)
        if self.every:
            slot = moment.replace(second=0, microsecond=0)
            slot -= timedelta(minutes=slot.minute % self.every)
            return (slot + timedelta(minutes=self.every)).astimezone(UTC)
        assert self.at
        hour, minute = (int(part) for part in self.at.split(":"))
        candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= moment:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)


CATEGORIES = {
    "conferencia-corpus": Category(
        "Notícias em aberto cujo artigo o corpus já tem: encerra como já conhecida, sem gastar IA.",
        "state IN ('pending','queued','retry','failed','cancelled') AND EXISTS "
        "(SELECT 1 FROM {corpus} c WHERE c.url_hash=bm_articles.hash)",
        every=10,
        change=_CLOSE_AS_KNOWN,
    ),
    "concluidas": Category(
        "Notícias gravadas ou já conhecidas: o corpus guarda o resultado.",
        f"{_SETTLED} AND outcome IN ('persisted','known')",
        days=3,
        at="03:30",
    ),
    "descartadas": Category(
        "Notícias descartadas (fora da janela, sem data, sem texto, conteúdo excluído, sem entidade).",
        f"{_SETTLED} AND outcome IN {_DISCARDED}",
        days=14,
        at="03:40",
    ),
    "corpo-descartadas": Category(
        "Texto da página das descartadas; a análise e os motivos continuam na linha.",
        "state='done' AND updated_at < now()-make_interval(days=>%(days)s::int) "
        f"AND outcome IN {_DISCARDED} AND payload ? 'body'",
        days=1,
        at="03:50",
        change="UPDATE bm_articles SET payload=payload-'body'",
    ),
}


class RetentionRepository(Repository):
    """Roda uma categoria de limpeza em lotes pequenos, cada um confirmado à parte."""

    def __init__(
        self,
        schema: str | None = None,
        env: Mapping[str, str] | None = None,
        *,
        corpus_schema: str = "public",
    ) -> None:
        """``corpus_schema`` é onde fica ``midias_ia_articles`` (``public`` em produção)."""
        super().__init__(schema, env)
        self.corpus_schema = corpus_schema

    def _sql(self, text: str) -> sql.Composed:
        """O SQL da categoria com ``{corpus}`` apontando para a tabela do corpus."""
        corpus = sql.Identifier(self.corpus_schema, "midias_ia_articles")
        return sql.SQL(text).format(corpus=corpus)

    def run(self, category: str, *, days: int | None = None, batch: int = BATCH) -> int:
        """Trata a categoria; devolve quantas linhas saíram (ou foram encerradas).

        A execução fica em ``bm_cleanup_runs`` desde o início, para a tela mostrar "em andamento",
        e termina com o total ou com o erro (que continua subindo).
        """
        spec = CATEGORIES.get(category)
        if spec is None:
            raise CatalogError("Categoria de limpeza desconhecida: " + ", ".join(CATEGORIES))
        if spec.days is None and days is not None:
            raise CatalogError("Esta categoria não usa idade mínima.")
        age = spec.days if days is None else days
        if age is not None and age < 1:
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
        statement = self._sql(spec.sql)
        try:
            while True:
                with self.connection() as db:
                    done = db.execute(statement, dict(days=age, batch=batch)).rowcount
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
        """Fecha o registro da execução e apara o histórico (por idade e por quantidade)."""
        with self.connection() as db:
            row = db.execute(
                "UPDATE bm_cleanup_runs SET finished_at=now(),removed=%s,error=%s WHERE id=%s "
                "RETURNING category",
                (removed, error, run_id),
            ).fetchone()
            assert row is not None
            db.execute(
                "DELETE FROM bm_cleanup_runs WHERE category=%(c)s AND (started_at < "
                "now()-make_interval(days=>%(days)s) OR id NOT IN (SELECT id FROM "
                "bm_cleanup_runs WHERE category=%(c)s ORDER BY id DESC LIMIT %(keep)s))",
                dict(c=row["category"], days=HISTORY_DAYS, keep=HISTORY_RUNS),
            )

    def status(self) -> list[dict[str, Any]]:
        """Para a tela: horário, última execução, próxima e quanto está esperando em cada categoria."""
        now = datetime.now(UTC)
        result = []
        with self.connection() as db:
            for name, spec in CATEGORIES.items():
                columns = "id,started_at,finished_at,days,removed,error"
                last = db.execute(
                    f"SELECT {columns} FROM bm_cleanup_runs WHERE category=%s "  # noqa: S608
                    "ORDER BY started_at DESC,id DESC LIMIT 1",
                    (name,),
                ).fetchone()
                # As frequentes rodam dezenas de vezes sem achar nada: o histórico guarda as que
                # mudaram algo, falharam ou ainda estão abertas.
                runs = db.execute(
                    f"SELECT {columns} FROM bm_cleanup_runs WHERE category=%s AND "  # noqa: S608
                    "(removed>0 OR error IS NOT NULL OR finished_at IS NULL) "
                    "ORDER BY started_at DESC,id DESC LIMIT %s",
                    (name, SHOWN_RUNS),
                ).fetchall()
                for run in [*runs, *([last] if last else [])]:
                    run["state"] = self._state(run, now)
                counted = db.execute(self._sql(spec.count_sql), dict(days=spec.days)).fetchone()
                assert counted is not None
                result.append(
                    dict(
                        category=name,
                        description=spec.description,
                        days=spec.days,
                        at=spec.at,
                        every=spec.every,
                        schedule=spec.schedule,
                        next_run=spec.next_run(now),
                        eligible=counted["n"],
                        last=last,
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
