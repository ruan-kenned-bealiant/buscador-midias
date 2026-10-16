"""Migração do estado antigo em SQLite (``var/state.db``) para as tabelas ``midias_crawler_*``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from buscador_midias.state.store import StateStore

_BATCH = 5000


class MigrationError(Exception):
    """A migração não pode (ou não deve) ser feita."""


def _when(value: str | None) -> datetime | None:
    """Data ISO 8601 do SQLite -> ``datetime`` com fuso."""
    return datetime.fromisoformat(value) if value else None


def _text(value: str | None) -> str | None:
    """Texto sem o caractere NUL, que o Postgres não aceita."""
    return value.replace("\x00", "") if value is not None else None


def _list(value: str | None) -> list[str]:
    """Texto separado por vírgula -> lista (vazio vira lista vazia)."""
    return [item for item in (value or "").split(",") if item]


def _entities(value: str) -> Jsonb:
    """Texto JSON do SQLite -> jsonb, sem NUL nos textos."""
    return Jsonb(json.loads(value.replace("\\u0000", "")))


# (tabela do SQLite, tabela do Postgres, colunas do SQLite em ordem, colunas do Postgres,
#  conversão da linha, ordem, preservar id). O ``id`` das rodadas é mantido porque outras tabelas
#  apontam para ele; o das URLs é gerado na ordem do ``rowid`` original.
_Convert = Callable[[tuple[Any, ...]], tuple[Any, ...]]
_TABLES: list[tuple[str, str, str, str, _Convert, str, bool]] = [
    (
        "runs",
        "midias_crawler_runs",
        "id, started_at, finished_at, bright_data_requests, bright_data_hosts",
        "id, started_at, finished_at, bright_data_requests, bright_data_hosts",
        lambda r: (r[0], _when(r[1]), _when(r[2]), r[3], r[4]),
        "id",
        True,
    ),
    (
        "enrich_runs",
        "midias_crawler_enrich_runs",
        "id, started_at, finished_at, model, prompt_version, evidence_mode, urls, fetched_ok,"
        " fetched_blocked, fetched_other, analyzed, findings, evidence_reused,"
        " evidence_brightdata, evidence_failed, bright_data_requests, prompt_tokens,"
        " completion_tokens, cost_usd, retries, errors",
        "id, started_at, finished_at, model, prompt_version, evidence_mode, urls, fetched_ok,"
        " fetched_blocked, fetched_other, analyzed, findings, evidence_reused,"
        " evidence_brightdata, evidence_failed, bright_data_requests, prompt_tokens,"
        " completion_tokens, cost_usd, retries, errors",
        lambda r: (r[0], _when(r[1]), _when(r[2]), *r[3:]),
        "id",
        True,
    ),
    (
        "corpus_runs",
        "midias_crawler_corpus_runs",
        "id, started_at, finished_at, candidates, created, existing, evidence_updated, entities,"
        " keywords, failed, rewritten",
        "id, started_at, finished_at, candidates, created, existing, evidence_updated, entities,"
        " keywords, failed, rewritten",
        lambda r: (r[0], _when(r[1]), _when(r[2]), *r[3:]),
        "id",
        True,
    ),
    (
        "urls_seen",
        "midias_crawler_urls",
        "url_hash, url, site_key, source_sitemap, kind, first_seen_run, first_seen_at, lastmod,"
        " title, publication_date, filter_reason",
        "url_hash, url, site_key, source_sitemap, kind, first_seen_run, first_seen_at, lastmod,"
        " title, publication_date, filter_reason",
        lambda r: (
            r[0],
            _text(r[1]),
            r[2],
            _text(r[3]),
            r[4],
            r[5],
            _when(r[6]),
            _when(r[7]),
            _text(r[8]),
            _when(r[9]),
            r[10],
        ),
        "rowid",
        False,
    ),
    (
        "site_kind_counts",
        "midias_crawler_site_kind_counts",
        "site_key, kind, n",
        "site_key, kind, n",
        lambda r: r,
        "site_key, kind",
        False,
    ),
    (
        "sitemap_state",
        "midias_crawler_sitemaps",
        "sitemap_url, site_key, lastmod, fetched_at",
        "sitemap_url, site_key, lastmod, fetched_at",
        lambda r: (_text(r[0]), r[1], _when(r[2]), _when(r[3])),
        "rowid",
        False,
    ),
    (
        "site_baseline",
        "midias_crawler_site_baselines",
        "site_key, run_id",
        "site_key, run_id",
        lambda r: r,
        "rowid",
        False,
    ),
    (
        "site_runs",
        "midias_crawler_site_runs",
        "run_id, site_key, status, new_urls, baseline_urls, backlog_urls, seen_urls,"
        " sitemaps_fetched, sitemaps_skipped, detail, known_urls, sitemaps_failed, filtered_urls,"
        " {source}",
        "run_id, site_key, status, new_urls, baseline_urls, backlog_urls, seen_urls,"
        " sitemaps_fetched, sitemaps_skipped, detail, known_urls, sitemaps_failed, filtered_urls,"
        " source",
        lambda r: (*r[:9], _text(r[9]), *r[10:]),
        "run_id, site_key",
        False,
    ),
    (
        "url_content",
        "midias_crawler_contents",
        "url_hash, status, http_status, final_url, title, description, lede, text_chars,"
        " published_at, language, error, attempts, fetched_at",
        "url_hash, status, http_status, final_url, title, description, lede, text_chars,"
        " published_at, language, error, attempts, fetched_at",
        lambda r: (
            r[0],
            r[1],
            r[2],
            _text(r[3]),
            _text(r[4]),
            _text(r[5]),
            _text(r[6]),
            r[7],
            _when(r[8]),
            r[9],
            _text(r[10]),
            r[11],
            _when(r[12]),
        ),
        "rowid",
        False,
    ),
    (
        "url_analysis",
        "midias_crawler_analyses",
        "url_hash, finding, themes, entities, adverse_fact, summary, confidence, basis, model,"
        " prompt_version, enrich_run_id, analyzed_at",
        "url_hash, finding, themes, entities, adverse_fact, summary, confidence, basis, model,"
        " prompt_version, enrich_run_id, analyzed_at",
        lambda r: (
            r[0],
            bool(r[1]),
            _list(r[2]),
            _entities(r[3]),
            _text(r[4]),
            _text(r[5]),
            r[6],
            r[7],
            r[8],
            r[9],
            r[10],
            _when(r[11]),
        ),
        "rowid",
        False,
    ),
    (
        "url_evidence",
        "midias_crawler_evidences",
        "url_hash, status, provider, path, sha256, size_bytes, content_type, http_status,"
        " final_url, rejected_reasons, error, attempts, updated_at",
        "url_hash, status, provider, path, sha256, size_bytes, content_type, http_status,"
        " final_url, rejected_reasons, error, attempts, updated_at",
        lambda r: (
            r[0],
            r[1],
            r[2],
            _text(r[3]),
            r[4],
            r[5],
            r[6],
            r[7],
            _text(r[8]),
            _list(r[9]),
            _text(r[10]),
            r[11],
            _when(r[12]),
        ),
        "rowid",
        False,
    ),
    (
        "corpus_writes",
        "midias_crawler_corpus_writes",
        "url_hash, article_id, created, evidence_state, written_at, corpus_run_id",
        "url_hash, article_id, created, evidence_state, written_at, corpus_run_id",
        lambda r: (r[0], r[1], bool(r[2]), r[3], _when(r[4]), r[5]),
        "rowid",
        False,
    ),
    (
        "fallback_hosts",
        "midias_crawler_fallback_hosts",
        "host, first_marked_at",
        "host, first_marked_at",
        lambda r: (r[0], _when(r[1])),
        "rowid",
        False,
    ),
]


def migrate_sqlite(path: Path, store: StateStore) -> dict[str, int]:
    """Copia o estado de um arquivo SQLite do buscador para o Postgres.

    Só migra para um estado vazio (nunca mistura nem duplica). Mantém os ids das rodadas, a ordem
    de chegada das URLs e todas as chaves; as tabelas da triagem por manchete e a coluna
    ``corpus_runs.withheld`` ficam de fora (não existem mais). Termina com os contadores
    automáticos de id logo depois do maior id migrado.

    Args:
        path: Arquivo SQLite (aberto somente leitura).
        store: Estado de destino (Postgres).

    Returns:
        Quantas linhas foram migradas, por tabela do SQLite.

    Raises:
        MigrationError: Se o arquivo não existe ou não é um estado do buscador, ou se o destino já
            tem dados.
    """
    if not path.is_file():
        raise MigrationError(f"arquivo não encontrado: {path}")
    if not store.is_empty():
        raise MigrationError("o estado no Postgres já tem dados; nada foi migrado")
    source = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        existing = {row[0] for row in source.execute("SELECT name FROM sqlite_master")}
        if not {"runs", "urls_seen"} <= existing:
            raise MigrationError(f"{path} não parece um estado do buscador (faltam runs/urls_seen)")
        counts: dict[str, int] = {}
        with store.transaction():  # tudo ou nada: uma falha no meio não deixa estado pela metade
            for lite, pg, lite_cols, pg_cols, convert, order, keep_id in _TABLES:
                if lite not in existing:
                    counts[lite] = 0
                    continue
                if lite == "site_runs":
                    columns = {row[1] for row in source.execute("PRAGMA table_info(site_runs)")}
                    lite_cols = lite_cols.format(
                        source="source" if "source" in columns else "'' AS source"
                    )
                counts[lite] = store.bulk_load(
                    pg,
                    [c.strip() for c in pg_cols.split(",")],
                    _rows(source, lite, lite_cols, order, convert),
                    keep_ids=keep_id,
                )
            store.restart_identities()
        return counts
    finally:
        source.close()


def _rows(
    source: sqlite3.Connection, table: str, columns: str, order: str, convert: _Convert
) -> Iterator[tuple[Any, ...]]:
    """Linhas do SQLite, já convertidas, na ordem pedida (``rowid`` = ordem de chegada)."""
    cursor = source.execute(f"SELECT {columns} FROM {table} ORDER BY {order}")
    while batch := cursor.fetchmany(_BATCH):
        for row in batch:
            yield convert(row)


def tables() -> Sequence[str]:
    """Nomes das tabelas do SQLite que a migração conhece."""
    return [entry[0] for entry in _TABLES]
