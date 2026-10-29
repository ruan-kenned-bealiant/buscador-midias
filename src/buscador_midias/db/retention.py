"""Limpeza periódica: o que já está no corpus ou perdeu a utilidade sai de ``bm_*``.

Cada categoria tem a sua idade e roda sozinha (um timer do systemd por categoria). A linha de uma
notícia concluída só diz "esta URL já foi vista"; quando é gravada ou já conhecida, o corpus
passa a dizer isso por ``url_hash`` e a linha pode sair. As descartadas guardam a memória que
evita ler a mesma página de novo, por isso ficam mais tempo. Falhas, trabalho em andamento e
``evidence_pending`` nunca são apagados aqui: esperam decisão ou ainda têm o que fazer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.connection import Repository

BATCH = 2000
MAX_BATCH = 20000
#: Pausa entre lotes: o Postgres é compartilhado com o PowerDD.
PAUSE_SECONDS = 0.2
_DISCARDED = "('outside_window','date_unknown','no_text','excluded_content','no_entities')"
_SETTLED = (
    "state='done' AND updated_at < now()-make_interval(days=>%(days)s::int) "
    "AND last_seen_at < now()-make_interval(days=>%(days)s::int)"
)


@dataclass(frozen=True)
class Category:
    """Uma categoria de limpeza: idade padrão em dias, o que é e o SQL de um lote."""

    days: int
    description: str
    sql: str


def _batch(table: str, key: str, where: str, change: str) -> str:
    """Um lote que não espera por linhas em uso: o que estiver travado fica para a próxima."""
    return (
        f"{change} WHERE {key} IN (SELECT {key} FROM {table} WHERE {where} "  # noqa: S608
        f"ORDER BY {key} LIMIT %(batch)s FOR UPDATE SKIP LOCKED)"
    )


CATEGORIES = {
    "concluidas": Category(
        3,
        "Notícias gravadas ou já conhecidas: o corpus guarda o resultado.",
        _batch(
            "bm_articles",
            "hash",
            f"{_SETTLED} AND outcome IN ('persisted','known')",
            "DELETE FROM bm_articles",
        ),
    ),
    "descartadas": Category(
        14,
        "Notícias descartadas (fora da janela, sem data, sem texto, conteúdo excluído, sem entidade).",
        _batch(
            "bm_articles",
            "hash",
            f"{_SETTLED} AND outcome IN {_DISCARDED}",
            "DELETE FROM bm_articles",
        ),
    ),
    "corpo-descartadas": Category(
        1,
        "Texto da página das descartadas; a análise e os motivos continuam na linha.",
        _batch(
            "bm_articles",
            "hash",
            "state='done' AND updated_at < now()-make_interval(days=>%(days)s::int) "
            f"AND outcome IN {_DISCARDED} AND payload ? 'body'",
            "UPDATE bm_articles SET payload=payload-'body'",
        ),
    ),
    "pedidos": Category(
        7,
        "Pedidos de reprocessamento antigos; as notícias deles só perdem o vínculo.",
        _batch(
            "bm_retry_requests",
            "id",
            "created_at < now()-make_interval(days=>%(days)s::int)",
            "DELETE FROM bm_retry_requests",
        ),
    ),
}


class RetentionRepository(Repository):
    """Roda uma categoria de limpeza em lotes pequenos, cada um confirmado à parte."""

    def run(self, category: str, *, days: int | None = None, batch: int = BATCH) -> int:
        """Limpa a categoria; devolve quantas linhas saíram (ou perderam o texto)."""
        spec = CATEGORIES.get(category)
        if spec is None:
            raise CatalogError("Categoria de limpeza desconhecida: " + ", ".join(CATEGORIES))
        age = spec.days if days is None else days
        if age < 1:
            raise CatalogError("A idade mínima de limpeza é 1 dia.")
        if not 1 <= batch <= MAX_BATCH:
            raise CatalogError(f"O lote deve ficar entre 1 e {MAX_BATCH}.")
        total = 0
        while True:
            with self.connection() as db:
                done = db.execute(spec.sql, dict(days=age, batch=batch)).rowcount
            total += done
            if done < batch:
                return total
            time.sleep(PAUSE_SECONDS)
