"""API local do buscador, sob o mesmo token e proteção de origem do gerenciador."""

from __future__ import annotations

import re
from typing import Any

from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion.store import IngestStore


def dispatch(
    api: Any, handler: Any, path: str, query: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    """Consulta filas e opera somente ingestão; não altera cadastro de sites/links."""
    repository = api.ingestion
    if not isinstance(repository, IngestStore):
        raise CatalogError("O buscador requer o catálogo PostgreSQL.", 503)
    if handler.command == "GET":
        source_corpus = re.fullmatch(r"/busca/fontes/([1-9][0-9]*)/corpus", path)
        if source_corpus:
            from buscador_midias.ingestion.progress import corpus_articles

            return 200, repository.jsonable(
                corpus_articles(
                    repository,
                    int(source_corpus[1]),
                    query.get("run_id", ""),
                    int(query.get("offset", "0")),
                )
            )
        if path == "/busca/historico":
            return 200, repository.jsonable(repository.archive_history())
        if path == "/busca/fontes":
            from buscador_midias.ingestion.progress import source_progress

            return 200, repository.jsonable(
                source_progress(
                    repository,
                    query.get("run_id", ""),
                    int(query.get("offset", "0")),
                    query.get("search", ""),
                )
            )
        if path == "/busca":
            return 200, repository.jsonable(
                repository.overview(
                    offset=int(query.get("offset", "0")),
                    stage=query.get("stage", ""),
                    state=query.get("state", ""),
                    run_id=query.get("run_id", ""),
                    source_id=int(query.get("source_id", "0")),
                    with_metrics=False,
                )
            )
        if path == "/busca/custos":
            return 200, repository.jsonable(
                dict(
                    metrics=repository.metrics(
                        run_id=query.get("run_id", ""),
                        source_id=int(query.get("source_id", "0")),
                    )
                )
            )
        match = re.fullmatch(r"/busca/noticias/([a-f0-9]{64})", path)
        if match:
            with repository.connection() as db:
                article = db.execute(
                    "SELECT * FROM ingest_articles WHERE hash=%s", (match[1],)
                ).fetchone()
                if not article:
                    raise CatalogError("Notícia não encontrada.", 404)
                if article.get("article_id") and not article["payload"].get("body"):
                    corpus = db.execute(
                        "SELECT body FROM public.midias_ia_articles WHERE id=%s",
                        (article["article_id"],),
                    ).fetchone()
                    if corpus:
                        article["payload"]["body"] = corpus["body"]
                origins = db.execute(
                    "SELECT o.*,p.link_id,p.snapshot FROM ingest_occurrences o "
                    "JOIN ingest_polls p ON p.id=o.poll_id WHERE o.hash=%s ORDER BY "
                    "o.created_at DESC",
                    (match[1],),
                ).fetchall()
            return 200, repository.jsonable(dict(article=article, origins=origins))
        raise CatalogError("Endereço não encontrado.", 404)
    api._authorize(handler)
    data = api._body(handler)
    if handler.command == "PATCH" and path == "/busca/configuracao":
        return 200, repository.jsonable(repository.configure(data))
    if handler.command == "POST" and path == "/busca/rodadas":
        return 202, repository.jsonable(repository.create_run(data))
    retry = re.fullmatch(r"/busca/tarefas/([a-f0-9]{32})/reprocessar", path)
    if handler.command == "POST" and retry and not data:
        return 202, repository.retry(retry[1])
    raise CatalogError("Endereço ou método não permitido.", 404)
