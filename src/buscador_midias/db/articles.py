"""Consulta dos artigos do corpus: entidades, contexto e alerta de cada uma, por site ou busca livre.

Lê só o corpus (``midias_ia_articles`` e as tabelas de entidades e contextos), nunca ``bm_articles``:
a linha da fila é apagada pela limpeza periódica, o artigo não.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.corpus.article import normalize_entity_name
from buscador_midias.corpus.theme_catalog import slug_of
from buscador_midias.db.connection import Repository

PAGE = 25
MAX_LIMIT = 100
MAX_OFFSET = 1_000_000
#: A contagem para de procurar aqui; a tela mostra "mais de 10.000".
COUNT_CAP = 10_000
MAX_ENTITIES = 40
SEARCHER = "buscador-midias"
FIELDS = ("all", "entity", "url", "title", "alert")
ZONE = ZoneInfo("America/Sao_Paulo")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
TIMEOUT = "20s"


def _like(text: str) -> str:
    r"""Padrão ``ILIKE`` que trata ``%``, ``_`` e ``\`` da busca como texto comum."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _day(value: str, label: str) -> date:
    if not _DAY.fullmatch(value):
        raise CatalogError(f"{label}: use a data no formato AAAA-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise CatalogError(f"{label}: data inexistente.") from error


class ArticleRepository(Repository):
    """Busca de artigos do corpus com as entidades e a análise de cada uma."""

    def __init__(
        self,
        schema: str | None = None,
        env: Mapping[str, str] | None = None,
        *,
        corpus_schema: str = "public",
    ) -> None:
        """``corpus_schema`` é onde ficam as tabelas ``midias_ia_*`` (``public`` em produção)."""
        super().__init__(schema, env)
        self.corpus_schema = corpus_schema

    def search(
        self,
        *,
        q: str = "",
        field: str = "all",
        site_id: int = 0,
        alert: str = "",
        since: str = "",
        until: str = "",
        scope: str = "searcher",
        offset: int = 0,
        limit: int = PAGE,
    ) -> dict[str, Any]:
        """Artigos por data de entrada (mais novos primeiro), já com entidades e análises.

        Args:
            q: Texto livre (2 a 100 caracteres). Entidade ignora acento e caixa.
            field: ``all`` (entidade, URL ou título), ``entity``, ``url``, ``title`` ou ``alert``
                (texto da justificativa e do fato adverso; mais lento, melhor com um período).
            site_id: Site do cadastro; 0 não filtra.
            alert: ``yes`` só com alerta, ``no`` só sem alerta, vazio qualquer.
            since: Primeira data de entrada, ``AAAA-MM-DD`` em Brasília.
            until: Última data de entrada (inclusive).
            scope: ``searcher`` (o que o buscador trouxe) ou ``all`` (todo o corpus).
            offset: Quantos artigos pular.
            limit: Artigos na página (1 a 100).
        """
        where, args = self._filters(q, field, alert, since, until, scope)
        if not isinstance(site_id, int) or site_id < 0:
            raise CatalogError("Site inválido.")
        if not 0 <= offset <= MAX_OFFSET or not 1 <= limit <= MAX_LIMIT:
            raise CatalogError("Paginação inválida.")
        try:
            with self.connection() as db:
                db.execute(f"SET LOCAL statement_timeout = '{TIMEOUT}'")
                if site_id:
                    where.append("(c.domain=%(domain)s OR c.source_domain=%(domain)s)")
                    args["domain"] = self._domain(db, site_id)
                condition = " AND ".join(where)
                total = self._query(
                    db,
                    f"SELECT count(*) n FROM (SELECT 1 FROM {{articles}} c WHERE {condition} "  # noqa: S608
                    f"LIMIT {COUNT_CAP + 1}) t",
                    args,
                ).fetchone()
                rows = self._query(
                    db,
                    "SELECT c.id,c.url_hash,c.url,c.titulo title,c.domain,c.source_name source,"
                    "c.published_at,c.created_at entered_at,c.origin,c.language,c.ai_verdict,"
                    "c.ai_score,c.evidencia_status,c.evidencia_path,(SELECT count(DISTINCT x.entity_name) "
                    "FROM {contexts} x WHERE x.article_id=c.id AND x.alerta) alert_count "
                    f"FROM {{articles}} c WHERE {condition} "  # noqa: S608
                    "ORDER BY c.created_at DESC,c.id DESC LIMIT %(limit)s OFFSET %(offset)s",
                    dict(args, limit=limit, offset=offset),
                ).fetchall()
                self._attach_entities(db, rows)
        except psycopg.errors.QueryCanceled as error:
            raise CatalogError(
                "A busca demorou demais. Reduza o período, escolha um site ou use um termo "
                "mais específico."
            ) from error
        assert total
        for row in rows:
            captured = row.pop("evidencia_status") == "captured" and row["evidencia_path"]
            row["evidence_url"] = f"/evidencias/corpus/{row['url_hash']}" if captured else None
            del row["evidencia_path"]
        return dict(
            items=rows,
            total=min(total["n"], COUNT_CAP),
            capped=total["n"] > COUNT_CAP,
            offset=offset,
            limit=limit,
        )

    # --- filtros -------------------------------------------------------------------------------

    def _filters(
        self, q: str, field: str, alert: str, since: str, until: str, scope: str
    ) -> tuple[list[str], dict[str, Any]]:
        if (
            field not in FIELDS
            or alert not in ("", "yes", "no")
            or scope not in ("searcher", "all")
        ):
            raise CatalogError("Filtro inválido.")
        q = q.strip()
        if q and not 2 <= len(q) <= 100:
            raise CatalogError("A busca deve ter de 2 a 100 caracteres.")
        where = ["TRUE"]
        args: dict[str, Any] = {}
        if scope == "searcher":
            where.append("c.first_seen_by=%(searcher)s")
            args["searcher"] = SEARCHER
        if since:
            where.append("c.created_at >= %(since)s")
            args["since"] = datetime.combine(_day(since, "Entrada a partir de"), time.min, ZONE)
        if until:
            where.append("c.created_at < %(until)s")
            args["until"] = datetime.combine(
                _day(until, "Entrada até") + timedelta(days=1), time.min, ZONE
            )
        if alert:
            negation = "" if alert == "yes" else "NOT "
            where.append(
                f"{negation}EXISTS (SELECT 1 FROM {{contexts}} a WHERE a.article_id=c.id AND a.alerta)"
            )
        if q:
            where.append(self._text(field))
            args.update(like=_like(q), norm=_like(normalize_entity_name(q)))
        return where, args

    @staticmethod
    def _text(field: str) -> str:
        """A condição da busca livre; ``all`` junta as demais."""
        clauses = {
            "entity": "EXISTS (SELECT 1 FROM {entities} e WHERE e.article_id=c.id "
            "AND e.name_norm LIKE %(norm)s)",
            "url": "c.url ILIKE %(like)s",
            "title": "c.titulo ILIKE %(like)s",
            "alert": "EXISTS (SELECT 1 FROM {contexts} x WHERE x.article_id=c.id AND "
            "(x.justificativa ILIKE %(like)s OR x.fato_adverso ILIKE %(like)s))",
        }
        if field != "all":
            return f"({clauses[field]})"
        # O texto da análise (1,2 milhão de linhas) fica de fora: é um campo à parte, escolhido.
        return "(" + " OR ".join(f"({clauses[name]})" for name in ("entity", "url", "title")) + ")"

    @staticmethod
    def _theme(row: dict[str, Any], names: dict[str, str]) -> str | None:
        """O tema pelo slug (nome de ``midias_ia_theme_prompts``); sem slug, o nome gravado.

        O gravador escreve o id do KyX, que muda com PF/PJ e nacional/estrangeira e traz o nome
        do KyX; o tema da análise é um só, o do slug.
        """
        slug = slug_of(row["theme_id"]) if row["theme_id"] is not None else None
        return names.get(slug or "") or row["theme"] or None

    def _domain(self, db: psycopg.Connection[dict[str, Any]], site_id: int) -> str:
        row = db.execute(
            "SELECT url_without_https_www d FROM midias_ia_trusted_sources "
            "WHERE id=%s AND identity IS NOT NULL",
            (site_id,),
        ).fetchone()
        if not row or not row["d"]:
            raise CatalogError("Site não encontrado.", 404)
        return str(row["d"]).strip().rstrip("/").lower()

    # --- consulta ------------------------------------------------------------------------------

    def _query(
        self, db: psycopg.Connection[dict[str, Any]], text: str, args: dict[str, Any]
    ) -> psycopg.Cursor[dict[str, Any]]:
        """Executa ``text`` com os nomes das tabelas do corpus no schema configurado."""
        names = {
            key: sql.Identifier(self.corpus_schema, table)
            for key, table in (
                ("articles", "midias_ia_articles"),
                ("entities", "midias_ia_article_entities"),
                ("contexts", "midias_ia_article_contexts"),
                ("prompts", "midias_ia_theme_prompts"),
            )
        }
        return db.execute(sql.SQL(text).format(**names), args)

    def _attach_entities(
        self, db: psycopg.Connection[dict[str, Any]], articles: list[dict[str, Any]]
    ) -> None:
        """Junta a cada artigo as entidades e, em cada entidade, as análises feitas sobre ela."""
        ids = [article["id"] for article in articles]
        entities = self._query(
            db,
            "SELECT article_id,name,name_norm,entity_type,role,mention_count,context "
            "FROM {entities} WHERE article_id=ANY(%(ids)s) ORDER BY article_id,id",
            dict(ids=ids),
        ).fetchall()
        analyses = self._query(
            db,
            "SELECT article_id,entity_name,theme_id,theme,alerta,confianca,justificativa,"
            "papel_entidade,fato_adverso,verification_status,ai_verdict FROM {contexts} "
            "WHERE article_id=ANY(%(ids)s) ORDER BY article_id,alerta DESC,confianca DESC,id",
            dict(ids=ids),
        ).fetchall()
        by_article: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
        for row in entities:
            key = row["name_norm"] or normalize_entity_name(row["name"])
            by_article[row["article_id"]].setdefault(
                key,
                dict(
                    name=row["name"],
                    type=row["entity_type"],
                    role=row["role"],
                    mentions=row["mention_count"],
                    context=row["context"],
                    alert=False,
                    analyses=[],
                ),
            )
        names = {
            row["slug"]: row["theme_name"]
            for row in self._query(
                db, "SELECT slug,theme_name FROM {prompts} WHERE slug IS NOT NULL", {}
            ).fetchall()
        }
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in analyses:
            key = normalize_entity_name(row["entity_name"] or "")
            entity = by_article[row["article_id"]].setdefault(
                key,
                dict(
                    name=row["entity_name"],
                    type=None,
                    role=None,
                    mentions=None,
                    context=None,
                    alert=False,
                    analyses=[],
                ),
            )
            entity["alert"] = entity["alert"] or bool(row["alerta"])
            # O gravador repete o mesmo parecer em uma linha por tema do catálogo (é assim que o
            # front do KyX lista); aqui um parecer igual aparece uma vez, com a lista de temas.
            content = (
                row["article_id"],
                key,
                bool(row["alerta"]),
                row["confianca"],
                row["justificativa"],
                row["fato_adverso"] or None,
                row["papel_entidade"],
                row["ai_verdict"],
                row["verification_status"],
            )
            analysis = merged.get(content)
            if analysis is None:
                analysis = merged[content] = dict(
                    themes=[],
                    alert=bool(row["alerta"]),
                    confidence=None if row["confianca"] is None else float(row["confianca"]),
                    justification=row["justificativa"],
                    adverse_fact=row["fato_adverso"] or None,
                    role=row["papel_entidade"],
                    verdict=row["ai_verdict"],
                    verification=row["verification_status"],
                )
                entity["analyses"].append(analysis)
            theme = self._theme(row, names)
            if theme and theme not in analysis["themes"]:
                analysis["themes"].append(theme)
        for article in articles:
            found = sorted(
                by_article.get(article["id"], {}).values(),
                key=lambda e: (not e["alert"], -(e["mentions"] or 0), str(e["name"]).lower()),
            )
            article["entities_total"] = len(found)
            article["entities"] = found[:MAX_ENTITIES]
