"""Cenário semeado para os testes de leitura: uma rodada com notícias em todas as situações."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from buscador_midias.db.catalog import CatalogRepository
from buscador_midias.db.health import HealthRepository

UNTIL = datetime.now(UTC)
SINCE = UNTIL - timedelta(hours=3)


def key(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def seed(schema: str) -> dict[str, Any]:
    """Dois sites, três links, rodada ``r1`` (aberta) e ``r0`` (fechada) com artigos e tarefas."""
    health = HealthRepository(schema)
    health.install()
    catalog = CatalogRepository(schema)
    sites = [
        catalog.save("source", dict(name="Alfa", url="https://alfa.example")),
        catalog.save("source", dict(name="Beta", url="https://beta.example")),
    ]
    links = [
        catalog.save(
            "endpoint",
            dict(url="https://alfa.example/feed", kind="feed", model="rss"),
            source_id=sites[0]["id"],
        ),
        catalog.save(
            "endpoint",
            dict(url="https://alfa.example/sitemap.xml", kind="sitemap", model="sitemap"),
            source_id=sites[0]["id"],
        ),
        catalog.save(
            "endpoint",
            dict(url="https://beta.example/feed", kind="feed", model="rss"),
            source_id=sites[1]["id"],
        ),
    ]
    with health.connection() as db:
        for run_id, status, created, finished in (
            ("r0", "completed", SINCE - timedelta(days=1), SINCE - timedelta(days=1, hours=-1)),
            ("r1", "running", SINCE, None),
        ):
            db.execute(
                "INSERT INTO bm_runs(id,type,trigger,status,config,since,until,created_at,"
                "finished_at,total) VALUES(%s,'search','scheduled',%s,%s,%s,%s,%s,%s,3)",
                (
                    run_id,
                    status,
                    Jsonb(dict(head_filter="enforce", adaptive_skipped=2, max_attempts=3)),
                    SINCE,
                    UNTIL,
                    created,
                    finished,
                ),
            )
        for run_id, link, done in (("r1", 0, True), ("r1", 1, False), ("r1", 2, True)):
            site = sites[0 if link < 2 else 1]
            db.execute(
                "INSERT INTO bm_run_links(run_id,link_id,site_id,host,discovery_done,state,"
                "listed,new_articles,known_articles) VALUES(%s,%s,%s,%s,%s,%s,10,6,4)",
                (
                    run_id,
                    links[link]["id"],
                    site["id"],
                    site["url"].removeprefix("https://"),
                    done,
                    "completed" if done else "running",
                ),
            )
        db.execute(
            "INSERT INTO bm_discovery_tasks(run_id,link_id,url,host,state,outcome,result) VALUES "
            "('r1',%s,'https://alfa.example/feed','alfa.example','done','urls_encontradas',%s),"
            "('r1',%s,'https://alfa.example/sitemap.xml','alfa.example','running',NULL,'{}'),"
            "('r1',%s,'https://beta.example/feed','beta.example','failed','http_503','{}')",
            (
                links[0]["id"],
                Jsonb(dict(metrics=dict(head_cut=3, not_modified_responses=1))),
                links[1]["id"],
                links[2]["id"],
            ),
        )
        rows = [
            # nome, site, link, stage, state, outcome, failed_stage, reason, reason_class, cost
            (
                "persisted",
                0,
                0,
                "persist",
                "done",
                "persisted",
                None,
                None,
                None,
                dict(
                    context=dict(cost_usd=0.02, prompt_tokens=100, completion_tokens=20),
                    fetch=dict(bright_data_requests=2),
                ),
            ),
            ("known", 0, 0, "dedup", "done", "known", None, None, None, {}),
            ("late", 0, 0, "fetch", "running", None, None, None, None, {}),
            ("waiting", 1, 2, "context", "pending", None, None, None, None, {}),
            ("blocked", 1, 2, "fetch", "failed", None, "fetch", "http_403", "blocked", {}),
            ("flaky", 1, 2, "fetch", "failed", None, "fetch", "http_503", "transient", {}),
            (
                "broken",
                0,
                0,
                "context",
                "failed",
                None,
                "context",
                "Falha interna",
                "bug",
                dict(context=dict(cost_usd=0.01, prompt_tokens=50)),
            ),
            ("nodate", 0, 0, "fetch", "done", "date_unknown", None, None, None, {}),
        ]
        for name, site, link, stage, state, outcome, failed, reason, klass, cost in rows:
            db.execute(
                "INSERT INTO bm_articles(hash,url,host,site_id,link_id,document_url,first_run_id,"
                "run_id,stage,state,outcome,failed_stage,reason,reason_class,retryable,failed_at,"
                "payload,cost,attempts) VALUES(%s,%s,%s,%s,%s,%s,'r1','r1',%s,%s,%s,%s,%s,%s,%s,"
                "CASE WHEN %s='failed' THEN now() END,%s,%s,1)",
                (
                    key(name),
                    f"https://{sites[site]['url'].removeprefix('https://')}/{name}",
                    sites[site]["url"].removeprefix("https://"),
                    sites[site]["id"],
                    links[link]["id"],
                    links[link]["url"],
                    stage,
                    state,
                    outcome,
                    failed,
                    reason,
                    klass,
                    klass in ("transient", "bug") if klass else None,
                    state,
                    Jsonb(dict(title=f"Título {name}")),
                    Jsonb(cost),
                ),
            )
    return dict(health=health, catalog=catalog, sites=sites, links=links)


#: O corpus como a tela Artigos o lê: artigos, entidades e a análise de cada entidade.
ARTICLE_CORPUS_DDL = """
CREATE TABLE midias_ia_articles (id BIGINT PRIMARY KEY, url_hash TEXT, url TEXT, titulo TEXT,
 domain TEXT, source_domain TEXT, source_name TEXT, origin TEXT, first_seen_by TEXT,
 published_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), evidencia_status TEXT,
 evidencia_path TEXT, ai_verdict TEXT, ai_score NUMERIC, language TEXT, body TEXT);
CREATE TABLE midias_ia_article_entities (id BIGSERIAL PRIMARY KEY, article_id BIGINT, name TEXT,
 name_norm TEXT, entity_type TEXT, role TEXT, salience NUMERIC, mention_count INTEGER,
 context TEXT);
CREATE TABLE midias_ia_article_contexts (id BIGSERIAL PRIMARY KEY, article_id BIGINT,
 entity_name TEXT, theme TEXT, alerta BOOLEAN, confianca NUMERIC, justificativa TEXT,
 papel_entidade TEXT, fato_adverso TEXT, verification_status TEXT, ai_verdict TEXT);
"""
