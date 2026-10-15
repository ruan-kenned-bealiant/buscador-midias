"""Gravação dos achados no corpus de Mídias IA (Postgres), direto, de forma idempotente.

Cada artigo é gravado em uma transação própria: uma falha desfaz só aquele artigo e não derruba o
lote. A chave natural é o ``url_hash`` (índice único do corpus), então regravar nunca duplica.

Regras de convivência com os outros escritores do corpus (KyX e importações):

- O artigo é inserido com ``ON CONFLICT (url_hash) DO NOTHING``: nunca sobrescreve o de ninguém.
- Artigo **nosso** (``origin = 'crawler'``): a evidência, as entidades e os temas acompanham a
  última análise (as entidades e os temas nossos são trocados, sem duplicar).
- Artigo de **outra origem**: só ganha a evidência se ainda não tinha uma capturada.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from buscador_midias.corpus.article import ORIGIN, SOURCE, CorpusArticle
from buscador_midias.corpus.index import ENV_VARS, CorpusConfigError

_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "midias_ia_articles": (
        "id",
        "url",
        "url_hash",
        "titulo",
        "resumo",
        "domain",
        "source_domain",
        "source_name",
        "source_country",
        "source_tier",
        "origin",
        "first_seen_by",
        "language",
        "published_at",
        "collected_at",
        "description",
        "body",
        "body_source",
        "ai_verdict",
        "ai_score",
        "ai_reason",
        "ai_summary",
        "ai_model",
        "ai_classified",
        "ai_classified_at",
        "evidencia_path",
        "evidencia_status",
        "evidencia_tipo",
        "evidencia_attempts",
        "created_at",
        "updated_at",
    ),
    "midias_ia_article_entities": (
        "article_id",
        "name",
        "name_norm",
        "entity_type",
        "role",
        "context",
        "extractor",
        "created_at",
        "updated_at",
    ),
    "midias_ia_article_keywords": (
        "article_id",
        "keyword",
        "category",
        "field",
        "weight",
        "created_at",
        "updated_at",
    ),
}
_INSERT_ARTICLE = (
    "INSERT INTO midias_ia_articles (url, url_hash, titulo, resumo, domain, source_domain,"
    " source_name, source_country, source_tier, origin, first_seen_by, language, published_at,"
    " collected_at, description, body, body_source, ai_verdict, ai_score, ai_reason, ai_summary,"
    " ai_model, ai_classified, ai_classified_at, evidencia_path, evidencia_status, evidencia_tipo,"
    " evidencia_attempts, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
    " %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, true, now(), %s, %s, %s, %s, now(), now())"
    " ON CONFLICT (url_hash) DO NOTHING RETURNING id"
)
_UPDATE_EVIDENCE = (
    "UPDATE midias_ia_articles SET evidencia_path = %s, evidencia_status = %s, evidencia_tipo = %s,"
    " evidencia_attempts = %s, updated_at = now() WHERE id = %s"
)
_INSERT_ENTITY = (
    "INSERT INTO midias_ia_article_entities (article_id, name, name_norm, entity_type, role,"
    " context, extractor, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())"
)
_INSERT_KEYWORD = (
    "INSERT INTO midias_ia_article_keywords (article_id, keyword, category, field, weight,"
    " created_at, updated_at) VALUES (%s, %s, %s, %s, %s, now(), now())"
)


class CorpusSchemaError(RuntimeError):
    """O esquema do corpus não tem as colunas que o gravador usa (a migration mudou?)."""


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """Resultado da gravação de um artigo.

    Attributes:
        url_hash: Hash do artigo.
        article_id: Id no corpus (``None`` se a gravação falhou).
        created: O artigo foi inserido agora.
        evidence_updated: A evidência de um artigo existente foi atualizada.
        entities: Entidades gravadas.
        keywords: Palavras-chave gravadas.
        error: Falha (tipo da exceção), se a gravação foi desfeita.
    """

    url_hash: str
    article_id: int | None
    created: bool
    evidence_updated: bool
    entities: int
    keywords: int
    error: str | None = None


class CorpusWriter(Protocol):
    """Porta: grava artigos no corpus."""

    def write(self, articles: Sequence[CorpusArticle]) -> list[WriteOutcome]:
        """Grava os artigos (um resultado por artigo, na mesma ordem)."""
        ...

    def existing(self, url_hashes: Sequence[str]) -> set[str]:
        """Dos hashes informados, quais existem no corpus."""
        ...

    def source_names(self) -> dict[str, str]:
        """Nome das fontes confiáveis ativas, por domínio."""
        ...


class PostgresCorpusWriter:
    """Grava no corpus de Mídias IA em Postgres (as mesmas tabelas que o KyX escreve e lê).

    Não é seguro para várias threads: use um por thread.
    """

    def __init__(self, connection: Any) -> None:
        """Cria o gravador.

        Args:
            connection: Conexão psycopg (sem autocommit; o gravador controla as transações).
        """
        self._connection = connection
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        schema: str | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> PostgresCorpusWriter:
        """Conecta com as variáveis ``DB_MIDIAS_IA_*`` do KyX e do powerdd-backend.

        Args:
            env: Variáveis de ambiente.
            schema: Schema onde procurar as tabelas (padrão: o do usuário; use só em testes).
            connect: Função de conexão (padrão: ``psycopg.connect``).

        Raises:
            CorpusConfigError: Se faltar variável (a mensagem nunca traz a senha).
        """
        missing = [name for name in ENV_VARS if not env.get(name)]
        if missing:
            raise CorpusConfigError(f"variáveis ausentes para o corpus: {', '.join(missing)}")
        if connect is None:
            import psycopg

            connect = psycopg.connect
        options = f"-c search_path={schema}" if schema else None
        connection = connect(
            host=env["DB_MIDIAS_IA_HOST"],
            port=int(env["DB_MIDIAS_IA_PORT"]),
            dbname=env["DB_MIDIAS_IA_DATABASE"],
            user=env["DB_MIDIAS_IA_USERNAME"],
            password=env["DB_MIDIAS_IA_PASSWORD"],
            options=options,
            autocommit=False,
        )
        return cls(connection)

    def check_schema(self) -> None:
        """Confere se as tabelas e colunas usadas existem, antes de gravar qualquer coisa.

        Raises:
            CorpusSchemaError: Listando o que falta.
        """
        missing: list[str] = []
        with self._lock, self._connection.cursor() as cursor:
            for table, columns in _REQUIRED_COLUMNS.items():
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = current_schema() AND table_name = %s",
                    (table,),
                )
                present = {row[0] for row in cursor.fetchall()}
                missing += [f"{table}.{column}" for column in columns if column not in present]
        self._connection.rollback()
        if missing:
            raise CorpusSchemaError(f"o corpus não tem: {', '.join(missing)}")

    def write(self, articles: Sequence[CorpusArticle]) -> list[WriteOutcome]:
        """Grava os artigos, cada um em sua transação; falhas viram ``error`` no resultado."""
        outcomes = []
        for article in articles:
            with self._lock:
                try:
                    with self._connection.transaction():
                        outcomes.append(self._write_one(article))
                except Exception as error:  # noqa: BLE001 - isola o artigo que falhou
                    outcomes.append(
                        WriteOutcome(
                            article.url_hash, None, False, False, 0, 0, type(error).__name__
                        )
                    )
        return outcomes

    def existing(self, url_hashes: Sequence[str]) -> set[str]:
        """Dos hashes informados, quais já estão no corpus."""
        if not url_hashes:
            return set()
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT url_hash FROM midias_ia_articles WHERE url_hash = ANY(%s)",
                (list(url_hashes),),
            )
            found = {row[0] for row in cursor.fetchall()}
        self._connection.rollback()
        return found

    def source_names(self) -> dict[str, str]:
        """Nome das fontes confiáveis ativas, por domínio (``url_without_https_www``)."""
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT url_without_https_www, name FROM midias_ia_trusted_sources WHERE active"
            )
            names = {row[0]: row[1] for row in cursor.fetchall() if row[0] and row[1]}
        self._connection.rollback()
        return names

    def close(self) -> None:
        """Fecha a conexão."""
        self._connection.close()

    def _write_one(self, article: CorpusArticle) -> WriteOutcome:
        """Grava um artigo (dentro da transação aberta por quem chama)."""
        with self._connection.cursor() as cursor:
            cursor.execute(_INSERT_ARTICLE, _article_values(article))
            inserted = cursor.fetchone()
            if inserted is not None:
                article_id = int(inserted[0])
                entities, keywords = _insert_children(cursor, article_id, article)
                return WriteOutcome(article.url_hash, article_id, True, False, entities, keywords)

            cursor.execute(
                "SELECT id, origin, evidencia_status FROM midias_ia_articles WHERE url_hash = %s",
                (article.url_hash,),
            )
            article_id, origin, current_status = cursor.fetchone()
            if origin == ORIGIN:
                return self._refresh_own(cursor, int(article_id), article)
            improved = current_status != "captured" and article.evidence.status == "captured"
            if improved:
                cursor.execute(_UPDATE_EVIDENCE, (*_evidence_values(article), article_id))
            return WriteOutcome(article.url_hash, int(article_id), False, improved, 0, 0)

    def _refresh_own(self, cursor: Any, article_id: int, article: CorpusArticle) -> WriteOutcome:
        """Atualiza um artigo nosso: evidência e, sem duplicar, entidades e temas."""
        cursor.execute(_UPDATE_EVIDENCE, (*_evidence_values(article), article_id))
        cursor.execute(
            "DELETE FROM midias_ia_article_entities WHERE article_id = %s AND extractor = %s",
            (article_id, SOURCE),
        )
        cursor.execute(
            "DELETE FROM midias_ia_article_keywords WHERE article_id = %s AND field = 'analysis'",
            (article_id,),
        )
        entities, keywords = _insert_children(cursor, article_id, article)
        return WriteOutcome(article.url_hash, article_id, False, True, entities, keywords)


def _text(value: str | None) -> str | None:
    """Texto sem bytes nulos (o Postgres os recusa); ``None`` continua ``None``."""
    return value.replace("\x00", "") if value is not None else None


def _evidence_values(article: CorpusArticle) -> tuple[Any, ...]:
    """Valores das colunas ``evidencia_*``."""
    evidence = article.evidence
    return (evidence.path, evidence.status, evidence.kind, evidence.attempts)


def _article_values(article: CorpusArticle) -> tuple[Any, ...]:
    """Parâmetros do INSERT do artigo, na ordem do SQL."""
    body = _text(article.body)
    return (
        _text(article.url),
        article.url_hash,
        _text(article.title),
        _text(article.summary),
        article.domain,
        article.source_domain,
        article.source_name,
        article.source_country,
        article.source_tier,
        article.origin,
        article.first_seen_by,
        article.language,
        article.published_at,
        _text(article.description),
        body,
        "crawler_html" if body else None,
        article.ai_verdict,
        article.ai_score,
        _text(article.ai_reason),
        _text(article.ai_summary),
        article.ai_model,
        *_evidence_values(article),
    )


def _insert_children(cursor: Any, article_id: int, article: CorpusArticle) -> tuple[int, int]:
    """Grava entidades e palavras-chave do artigo; devolve quantas de cada."""
    for entity in article.entities:
        cursor.execute(
            _INSERT_ENTITY,
            (
                article_id,
                _text(entity.name),
                entity.name_norm,
                entity.entity_type,
                entity.role,
                _text(entity.context),
                SOURCE,
            ),
        )
    for keyword in article.keywords:
        cursor.execute(
            _INSERT_KEYWORD,
            (article_id, keyword.keyword, keyword.category, keyword.field, keyword.weight),
        )
    return len(article.entities), len(article.keywords)
