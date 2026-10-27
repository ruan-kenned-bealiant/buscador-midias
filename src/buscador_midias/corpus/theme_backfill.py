"""Retroativo: contextos gravados sem tema ganham o tema da notícia, sem chamar a IA.

Antes de o tema ser gravado, cada entidade tinha uma linha sem ``theme_id``, que o front não lista
nas buscas por tema. Aqui cada uma dessas linhas (só as deste escritor e sem vínculo de processo,
cliente ou grupo) vira uma linha por tema do catálogo, copiando o parecer individual, a partir das
palavras-chave do próprio artigo.
"""

from __future__ import annotations

from typing import Any

from buscador_midias.corpus.article import SOURCE
from buscador_midias.corpus.contexts import CONTEXT_COLUMNS
from buscador_midias.corpus.theme_catalog import ThemeRef, theme_refs

_COPIED = tuple(
    c for c in CONTEXT_COLUMNS if c not in ("theme_id", "theme", "theme_description", "updated_at")
)
_UNTHEMED = (
    "SELECT id,article_id,entity_type FROM midias_ia_article_contexts WHERE theme_id IS NULL "
    "AND process_id IS NULL AND client_id IS NULL AND group_id IS NULL "
    "AND raw_context->>'producer'=%s ORDER BY id"
)


def _plan(cursor: Any) -> list[tuple[int, tuple[ThemeRef, ...]]]:
    """Cada linha sem tema com os temas do catálogo que as palavras-chave do artigo apontam."""
    cursor.execute(_UNTHEMED, (SOURCE,))
    unthemed = cursor.fetchall()
    cursor.execute(
        "SELECT article_id,category FROM midias_ia_article_keywords WHERE article_id=ANY(%s)",
        (list({row[1] for row in unthemed}),),
    )
    slugs: dict[int, list[str]] = {}
    for article_id, category in cursor.fetchall():
        slugs.setdefault(article_id, []).append(category)
    plan = []
    for context_id, article_id, kind in unthemed:
        entity_type = "PF" if kind == "pf" else "PJ"
        refs = {
            ref.theme_id: ref
            for slug in slugs.get(article_id, [])
            for ref in theme_refs(slug, entity_type)
        }
        plan.append((context_id, tuple(refs.values())))
    return plan


def backfill_themes(connection: Any, *, apply: bool = False, batch: int = 500) -> dict[str, int]:
    """Relatório sempre; troca as linhas sem tema pelas temáticas só com ``apply``.

    Cada lote é uma transação: um erro desfaz o lote inteiro e interrompe o processo.
    """
    with connection.cursor() as cursor:
        plan = _plan(cursor)
    themable = [(context_id, refs) for context_id, refs in plan if refs]
    report = dict(
        rows=len(plan),
        themed_rows=sum(len(refs) for _, refs in themable),
        applied=0,
    )
    if not apply:
        return report
    columns = ",".join(_COPIED)
    for start in range(0, len(themable), batch):
        with connection.transaction(), connection.cursor() as cursor:
            for context_id, refs in themable[start : start + batch]:
                for ref in refs:
                    cursor.execute(
                        f"INSERT INTO midias_ia_article_contexts ({columns},theme_id,theme,"
                        f"theme_description,updated_at) SELECT {columns},%s,%s,%s,now() "
                        "FROM midias_ia_article_contexts WHERE id=%s "
                        "ON CONFLICT (article_id,(COALESCE(theme_id,0)),entity_hash) DO NOTHING",
                        (ref.theme_id, ref.name, ref.description, context_id),
                    )
                cursor.execute("DELETE FROM midias_ia_article_contexts WHERE id=%s", (context_id,))
                report["applied"] += 1
    return report
