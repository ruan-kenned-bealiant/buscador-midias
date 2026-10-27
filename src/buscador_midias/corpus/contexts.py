"""Persistência de pareceres individuais no contrato compartilhado com KyX/powerDD."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from typing import Any

from psycopg.types.json import Json

from buscador_midias.analysis.prompt import PROMPT_VERSION
from buscador_midias.corpus.article import SOURCE, CorpusArticle, normalize_entity_name
from buscador_midias.corpus.theme_catalog import ThemeRef, theme_refs

CONTEXT_COLUMNS = (
    "article_id",
    "process_id",
    "client_id",
    "group_id",
    "theme_id",
    "theme",
    "theme_description",
    "entity_name",
    "entity_document",
    "entity_type",
    "entity_hash",
    "match_nome",
    "tema_confirmado",
    "alerta",
    "confianca",
    "justificativa",
    "papel_entidade",
    "fato_adverso",
    "termos_relacionados",
    "raw_context",
    "ai_verdict",
    "ai_score",
    "ai_reason",
    "ai_summary",
    "ai_model",
    "evaluated_at",
    "created_at",
    "updated_at",
)


def entity_identity(name: str, entity_type: str) -> tuple[str, str]:
    """Replica tipo|nome sanitizado do KyX sem atribuir CPF/CNPJ a uma menção."""
    kind = "pf" if entity_type == "PF" else "pj"
    identity = re.sub(r"[^0-9A-Za-z]", "", name).lower()
    # Evita juntar todas as identidades não latinas em hash('pf|')/hash('pj|').
    if not identity:
        identity = "name-sha256:" + hashlib.sha256(name.casefold().encode()).hexdigest()
    return kind, hashlib.sha256(f"{kind}|{identity}".encode()).hexdigest()


def article_themes(article: CorpusArticle, entity_type: str) -> tuple[ThemeRef, ...]:
    """Temas do catálogo da notícia (nas variantes do tipo da entidade), sem repetir ``theme_id``.

    Toda entidade citada recebe os temas da notícia; o ``alerta`` continua sendo individual.
    """
    refs = (ref for k in article.keywords for ref in theme_refs(k.category, entity_type))
    return tuple({ref.theme_id: ref for ref in refs}.values())


def insert_contexts(cursor: Any, article_id: int, article: CorpusArticle) -> int:
    """Upsert idempotente, sem substituir avaliação de processo/cliente ou outro escritor.

    Grava uma linha por entidade e por tema da notícia (``theme_id`` do catálogo do KyX), que é o
    que o front usa para listar; sem tema do catálogo, uma única linha sem ``theme_id``.
    """
    count = 0
    entities = {e.name_norm for e in article.entities}
    keyed = {}
    for context in article.entity_contexts:
        if normalize_entity_name(context.name) not in entities:
            raise ValueError("Contexto sem entidade correspondente no artigo")
        kind, key = entity_identity(context.name, context.entity_type)
        keyed[key] = (kind, context)
    for key, (kind, context) in sorted(keyed.items()):
        themes: tuple[ThemeRef | None, ...] = article_themes(article, context.entity_type) or (
            None,
        )
        if themes[0] is not None:
            # A linha sem tema deste escritor é substituída pelas temáticas, nunca somada a elas.
            cursor.execute(
                "DELETE FROM midias_ia_article_contexts WHERE article_id=%s AND entity_hash=%s "
                "AND theme_id IS NULL AND process_id IS NULL AND client_id IS NULL "
                "AND group_id IS NULL AND raw_context->>'producer'=%s",
                (article_id, key, SOURCE),
            )
        raw = dict(
            {
                k: v.replace("\x00", "") if isinstance(v, str) else v
                for k, v in asdict(context).items()
            },
            producer=SOURCE,
            prompt_version=article.context_prompt_version or PROMPT_VERSION,
            scope="article_entity",
            identity_basis="name_only",
            civil_identity_confirmed=False,
        )
        for theme in themes:
            cursor.execute(
                "INSERT INTO midias_ia_article_contexts (article_id,theme_id,theme,"
                "theme_description,entity_name,entity_document,entity_type,entity_hash,"
                "match_nome,tema_confirmado,alerta,confianca,justificativa,papel_entidade,"
                "fato_adverso,termos_relacionados,raw_context,ai_verdict,ai_score,ai_reason,"
                "ai_summary,ai_model,evaluated_at,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,true,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "now(),now(),now()) "
                "ON CONFLICT (article_id,(COALESCE(theme_id,0)),entity_hash) DO UPDATE SET "
                "entity_name=excluded.entity_name,entity_type=excluded.entity_type,"
                "match_nome=excluded.match_nome,tema_confirmado=excluded.tema_confirmado,"
                "alerta=excluded.alerta,confianca=excluded.confianca,"
                "justificativa=excluded.justificativa,papel_entidade=excluded.papel_entidade,"
                "fato_adverso=excluded.fato_adverso,"
                "termos_relacionados=excluded.termos_relacionados,"
                "raw_context=excluded.raw_context,ai_verdict=excluded.ai_verdict,"
                "ai_score=excluded.ai_score,ai_reason=excluded.ai_reason,"
                "ai_summary=excluded.ai_summary,ai_model=excluded.ai_model,"
                "evaluated_at=now(),updated_at=now() "
                "WHERE midias_ia_article_contexts.process_id IS NULL "
                "AND midias_ia_article_contexts.client_id IS NULL "
                "AND midias_ia_article_contexts.group_id IS NULL "
                "AND midias_ia_article_contexts.raw_context->>'producer'=%s",
                (
                    article_id,
                    theme.theme_id if theme else None,
                    theme.name if theme else None,
                    theme.description if theme else None,
                    context.name.replace("\x00", ""),
                    kind,
                    key,
                    context.alert,
                    context.confidence,
                    context.reason.replace("\x00", ""),
                    context.role,
                    context.adverse_fact.replace("\x00", ""),
                    Json(list(context.themes)),
                    Json(raw),
                    "relevante" if context.alert else "irrelevante",
                    context.confidence,
                    context.reason.replace("\x00", ""),
                    context.reason.replace("\x00", ""),
                    article.ai_model,
                    SOURCE,
                ),
            )
            count += cursor.rowcount
    return count
