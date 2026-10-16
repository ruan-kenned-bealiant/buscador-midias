"""Banco de testes: um schema novo por teste, criado com o mesmo DDL da migration.

O estado do buscador vive no Postgres. Os testes usam um banco próprio (``buscador_midias_test``,
nunca o do corpus) no mesmo servidor do ``DB_MIDIAS_IA_*``, e cada teste ganha um schema descartável
com as tabelas de ``sql/postgres/00*.sql``. As credenciais vêm do
ambiente ou, na falta delas, do ``.env`` do KyX (só as variáveis de conexão).
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
DDL = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted((ROOT / "sql" / "postgres").glob("00*.sql"))
)
TEST_DATABASE = "buscador_midias_test"
_KEYS = ("DB_MIDIAS_IA_PORT", "DB_MIDIAS_IA_USERNAME", "DB_MIDIAS_IA_PASSWORD")


def _credentials() -> dict[str, str] | None:
    """Porta, usuário e senha do servidor de testes (ambiente ou .env do KyX); ``None`` sem eles."""
    values = {key: os.environ[key] for key in _KEYS if os.environ.get(key)}
    env_file = Path(os.environ.get("KYX_ENV", ROOT.parent / "KyX-NovoDD-backend" / ".env"))
    if len(values) < len(_KEYS) and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key in _KEYS and key not in values:
                values[key] = value.strip().strip("\"'")
    if len(values) < len(_KEYS):
        return None
    return {
        "host": os.environ.get("TEST_DB_HOST", "127.0.0.1"),
        "port": values["DB_MIDIAS_IA_PORT"],
        "user": values["DB_MIDIAS_IA_USERNAME"],
        "password": values["DB_MIDIAS_IA_PASSWORD"],
    }


@pytest.fixture(scope="session")
def pg_server() -> dict[str, str] | None:
    """Parâmetros do servidor de testes, com o banco de testes criado; ``None`` se indisponível."""
    credentials = _credentials()
    if credentials is None:
        return None
    try:
        with psycopg.connect(
            dbname="postgres", autocommit=True, connect_timeout=3, **credentials
        ) as admin:
            exists = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE,)
            ).fetchone()
            if not exists:
                admin.execute(f'CREATE DATABASE "{TEST_DATABASE}"')
    except psycopg.Error:
        return None
    return {**credentials, "dbname": TEST_DATABASE}


@pytest.fixture(autouse=True)
def state_schema(
    pg_server: dict[str, str] | None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str | None]:
    """Cria um schema com as tabelas do estado e aponta o ambiente para ele durante o teste."""
    if pg_server is None:
        yield None
        return
    schema = f"t_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(autocommit=True, **pg_server) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        conn.execute(DDL)
    monkeypatch.setenv("DB_MIDIAS_IA_HOST", pg_server["host"])
    monkeypatch.setenv("DB_MIDIAS_IA_PORT", pg_server["port"])
    monkeypatch.setenv("DB_MIDIAS_IA_DATABASE", pg_server["dbname"])
    monkeypatch.setenv("DB_MIDIAS_IA_USERNAME", pg_server["user"])
    monkeypatch.setenv("DB_MIDIAS_IA_PASSWORD", pg_server["password"])
    monkeypatch.setenv("BUSCADOR_STATE_SCHEMA", schema)
    yield schema
    with psycopg.connect(autocommit=True, **pg_server) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


LEGACY = """
CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
    finished_at TEXT, bright_data_requests INTEGER NOT NULL DEFAULT 0,
    bright_data_hosts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE urls_seen (url_hash TEXT PRIMARY KEY, url TEXT NOT NULL, site_key TEXT NOT NULL,
    source_sitemap TEXT NOT NULL, kind TEXT NOT NULL, first_seen_run INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL, lastmod TEXT, title TEXT, publication_date TEXT,
    filter_reason TEXT);
CREATE TABLE site_kind_counts (site_key TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (site_key, kind));
CREATE TABLE sitemap_state (sitemap_url TEXT PRIMARY KEY, site_key TEXT NOT NULL, lastmod TEXT,
    fetched_at TEXT NOT NULL);
CREATE TABLE site_baseline (site_key TEXT PRIMARY KEY, run_id INTEGER NOT NULL);
CREATE TABLE site_runs (run_id INTEGER NOT NULL, site_key TEXT NOT NULL, status TEXT NOT NULL,
    new_urls INTEGER NOT NULL, baseline_urls INTEGER NOT NULL, backlog_urls INTEGER NOT NULL,
    seen_urls INTEGER NOT NULL, sitemaps_fetched INTEGER NOT NULL,
    sitemaps_skipped INTEGER NOT NULL, detail TEXT NOT NULL, known_urls INTEGER NOT NULL DEFAULT 0,
    sitemaps_failed INTEGER NOT NULL DEFAULT 0, filtered_urls INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '', PRIMARY KEY (run_id, site_key));
CREATE TABLE url_content (url_hash TEXT PRIMARY KEY, status TEXT NOT NULL, http_status INTEGER,
    final_url TEXT, title TEXT, description TEXT, lede TEXT, text_chars INTEGER NOT NULL DEFAULT 0,
    published_at TEXT, language TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL);
CREATE TABLE enrich_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
    finished_at TEXT, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
    evidence_mode TEXT NOT NULL, urls INTEGER NOT NULL DEFAULT 0,
    fetched_ok INTEGER NOT NULL DEFAULT 0, fetched_blocked INTEGER NOT NULL DEFAULT 0,
    fetched_other INTEGER NOT NULL DEFAULT 0, analyzed INTEGER NOT NULL DEFAULT 0,
    findings INTEGER NOT NULL DEFAULT 0, evidence_reused INTEGER NOT NULL DEFAULT 0,
    evidence_brightdata INTEGER NOT NULL DEFAULT 0, evidence_failed INTEGER NOT NULL DEFAULT 0,
    bright_data_requests INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL, retries INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0);
CREATE TABLE url_analysis (url_hash TEXT PRIMARY KEY, finding INTEGER NOT NULL,
    themes TEXT NOT NULL, entities TEXT NOT NULL, adverse_fact TEXT NOT NULL,
    summary TEXT NOT NULL, confidence REAL NOT NULL, basis TEXT NOT NULL, model TEXT NOT NULL,
    prompt_version TEXT NOT NULL, enrich_run_id INTEGER NOT NULL, analyzed_at TEXT NOT NULL);
CREATE TABLE url_evidence (url_hash TEXT PRIMARY KEY, status TEXT NOT NULL, provider TEXT,
    path TEXT, sha256 TEXT, size_bytes INTEGER NOT NULL DEFAULT 0, content_type TEXT,
    http_status INTEGER, final_url TEXT, rejected_reasons TEXT NOT NULL DEFAULT '', error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE corpus_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
    finished_at TEXT, candidates INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL DEFAULT 0,
    existing INTEGER NOT NULL DEFAULT 0, evidence_updated INTEGER NOT NULL DEFAULT 0,
    entities INTEGER NOT NULL DEFAULT 0, keywords INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0, withheld INTEGER NOT NULL DEFAULT 0,
    rewritten INTEGER NOT NULL DEFAULT 0);
CREATE TABLE corpus_writes (url_hash TEXT PRIMARY KEY, article_id INTEGER, created INTEGER NOT NULL,
    evidence_state TEXT NOT NULL, written_at TEXT NOT NULL, corpus_run_id INTEGER NOT NULL);
CREATE TABLE fallback_hosts (host TEXT PRIMARY KEY, first_marked_at TEXT NOT NULL);
CREATE TABLE triage_runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL);
CREATE TABLE url_triage (url_hash TEXT PRIMARY KEY, decision TEXT NOT NULL);
"""

TS = "2026-10-15T16:14:36.990314+00:00"
ENTITIES = json.dumps([{"name": "João Silva", "type": "PF", "role": "acusado"}])


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    """Estado antigo em SQLite (o formato de antes da migração para o Postgres)."""
    path = tmp_path / "antigo.db"
    db = sqlite3.connect(path)
    db.executescript(LEGACY)
    db.executescript(
        f"""
        INSERT INTO runs VALUES (7, '{TS}', '{TS}', 5, 2);
        INSERT INTO urls_seen VALUES ('h-1', 'https://a.com/um', 'a.com', 'https://a.com/s.xml',
            'new', 7, '{TS}', NULL, 'Título' || char(0) || 'sujo',
            '2026-10-15T16:09:11+00:00', NULL);
        INSERT INTO urls_seen VALUES ('h-0', 'https://a.com/zero', 'a.com', 'feed:a.com',
            'baseline', 7, '{TS}', '{TS}', NULL, NULL, NULL);
        INSERT INTO urls_seen VALUES ('h-2', 'https://a.com/tag/x', 'a.com', 's', 'filtered', 7,
            '{TS}', NULL, NULL, NULL, 'listing');
        INSERT INTO site_kind_counts VALUES ('a.com', 'new', 1), ('a.com', 'baseline', 1);
        INSERT INTO sitemap_state VALUES ('https://a.com/s.xml', 'a.com', '{TS}', '{TS}');
        INSERT INTO site_baseline VALUES ('a.com', 7);
        INSERT INTO site_runs VALUES (7, 'a.com', 'ok', 1, 1, 0, 3, 2, 0, '', 0, 0, 1, 'feed');
        INSERT INTO url_content VALUES ('h-1', 'ok', 200, NULL, 'T', 'D', 'lede', 900,
            '{TS}', 'pt-BR', NULL, 1, '{TS}');
        INSERT INTO enrich_runs (id, started_at, finished_at, model, prompt_version, evidence_mode,
            cost_usd) VALUES (3, '{TS}', '{TS}', 'm', 'v', 'auto', 0.0706);
        INSERT INTO url_analysis VALUES ('h-1', 1, 'fiscal,criminal', '{ENTITIES}', 'fato', 'res',
            0.9, 'text', 'm', 'v', 3, '{TS}');
        INSERT INTO url_evidence VALUES ('h-1', 'failed', NULL, NULL, NULL, 0, NULL, 200, NULL,
            'thin_content,paywall', 'x', 2, '{TS}');
        INSERT INTO corpus_runs (id, started_at, withheld) VALUES (4, '{TS}', 9);
        INSERT INTO corpus_writes VALUES ('h-1', 75053, 1, 'captured', '{TS}', 4);
        INSERT INTO fallback_hosts VALUES ('apnews.com', '{TS}');
        INSERT INTO triage_runs VALUES (1, '{TS}');
        INSERT INTO url_triage VALUES ('h-1', 'keep');
        """
    )
    db.commit()
    db.close()
    return path
