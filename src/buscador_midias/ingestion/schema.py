"""DDL aditivo da ingestão, separado do corpus e das rodadas históricas."""

STAGES = ("discovery", "dedup", "fetch", "context", "evidence", "persist")
CONTENT_FILTER_MODES = ("off", "shadow", "enforce")
# Notícia e opinião nunca são bloqueadas: o restante é rotina sem interesse para o corpus.
BLOCKABLE_CATEGORIES = ("weather", "cooking", "sports", "entertainment", "lifestyle", "horoscope")
DEFAULTS = dict(
    enabled=False,
    workers_enabled=True,
    window_mode="today",
    timezone="America/Sao_Paulo",
    schedule_mode="clock",
    schedule_hours=[1, 6, 12, 16],
    daily_routes_only=True,
    lookback_minutes=2880,
    allow_undated=False,
    bright_data=False,
    evidence_paid_adverse_only=True,
    max_attempts=3,
    retry_minutes=1,
    model="openai/gpt-4.1-mini",
    analysis_text_chars=12000,
    content_filter="shadow",
    blocked_content_categories=["horoscope", "weather", "cooking", "sports"],
    workers=dict(discovery=2, dedup=1, fetch=2, context=1, evidence=1, persist=1),
)
DDL = """
CREATE TABLE IF NOT EXISTS ingest_daily_routes (
 link_id BIGINT PRIMARY KEY REFERENCES links(id),purpose TEXT NOT NULL
 CHECK(purpose IN ('updates','archive','review')),reason TEXT NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ingest_archives (
 schema_name TEXT PRIMARY KEY,queue_prefix TEXT NOT NULL,manifest JSONB NOT NULL,
 archived_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ingest_settings (
 id INT PRIMARY KEY CHECK(id=1),version INT NOT NULL DEFAULT 1,
 config JSONB NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ingest_runs (
 id TEXT PRIMARY KEY,trigger TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'running',
 since TIMESTAMPTZ NOT NULL,until TIMESTAMPTZ NOT NULL,config JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),finished_at TIMESTAMPTZ,
 CHECK(since<=until)
);
CREATE TABLE IF NOT EXISTS ingest_clock_runs (
 slot TIMESTAMPTZ PRIMARY KEY,run_id TEXT NOT NULL UNIQUE REFERENCES ingest_runs(id)
);
CREATE TABLE IF NOT EXISTS ingest_schedule (
 link_id BIGINT PRIMARY KEY REFERENCES links(id),next_due TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_started TIMESTAMPTZ,last_finished TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS ingest_polls (
 id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES ingest_runs(id),
 link_id BIGINT NOT NULL REFERENCES links(id),snapshot JSONB NOT NULL,
 discovery_done BOOLEAN NOT NULL DEFAULT false,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(run_id,link_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ingest_one_read ON ingest_polls(link_id) WHERE NOT discovery_done;
CREATE TABLE IF NOT EXISTS ingest_articles (
 hash TEXT PRIMARY KEY,url TEXT NOT NULL,source_id BIGINT NOT NULL REFERENCES sites(id),
 run_id TEXT NOT NULL REFERENCES ingest_runs(id),poll_id TEXT NOT NULL REFERENCES ingest_polls(id),
 generation INT NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'pending',
 payload JSONB NOT NULL DEFAULT '{}',article_id BIGINT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ingest_occurrences (
 run_id TEXT NOT NULL REFERENCES ingest_runs(id),poll_id TEXT NOT NULL REFERENCES ingest_polls(id),
 hash TEXT NOT NULL REFERENCES ingest_articles(hash),document_url TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),PRIMARY KEY(poll_id,hash)
);
CREATE INDEX IF NOT EXISTS ingest_occurrence_hash ON ingest_occurrences(hash,run_id);
CREATE INDEX IF NOT EXISTS ingest_article_publication_proof ON ingest_articles(source_id)
 WHERE payload->>'published_at_basis'='html';
CREATE TABLE IF NOT EXISTS ingest_jobs (
 id TEXT PRIMARY KEY,stage TEXT NOT NULL CHECK(stage IN
 ('discovery','dedup','fetch','context','evidence','persist')),
 subject TEXT NOT NULL,generation INT NOT NULL DEFAULT 1,
 run_id TEXT NOT NULL REFERENCES ingest_runs(id),poll_id TEXT NOT NULL REFERENCES ingest_polls(id),
 hash TEXT REFERENCES ingest_articles(hash),state TEXT NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','queued','running','retry','succeeded','failed','skipped','cancelled')),

 payload JSONB NOT NULL DEFAULT '{}',result JSONB NOT NULL DEFAULT '{}',outcome TEXT,
 attempts INT NOT NULL DEFAULT 0,failures INT NOT NULL DEFAULT 0,dispatch INT NOT NULL DEFAULT
1,owner TEXT,
 lease_until TIMESTAMPTZ,available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),started_at TIMESTAMPTZ,finished_at TIMESTAMPTZ,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),UNIQUE(stage,subject,generation)
);
CREATE INDEX IF NOT EXISTS ingest_job_queue ON ingest_jobs(stage,state,available_at);
CREATE INDEX IF NOT EXISTS ingest_job_poll ON ingest_jobs(poll_id,stage,state);
CREATE INDEX IF NOT EXISTS ingest_job_hash ON ingest_jobs(hash,generation);
CREATE TABLE IF NOT EXISTS ingest_attempts (
 job_id TEXT NOT NULL REFERENCES ingest_jobs(id),attempt INT NOT NULL,owner TEXT NOT NULL,
 started_at TIMESTAMPTZ NOT NULL DEFAULT now(),finished_at TIMESTAMPTZ,outcome TEXT,
 detail TEXT NOT NULL DEFAULT '',metrics JSONB NOT NULL DEFAULT '{}',
 PRIMARY KEY(job_id,attempt)
);
CREATE TABLE IF NOT EXISTS ingest_outbox (
 id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES ingest_jobs(id),dispatch INT NOT NULL,kind TEXT NOT NULL
DEFAULT 'work',
 ready_at TIMESTAMPTZ NOT NULL DEFAULT now(),published_at TIMESTAMPTZ,
 UNIQUE(job_id,dispatch)
);
CREATE INDEX IF NOT EXISTS ingest_outbox_pending ON ingest_outbox(ready_at)
 WHERE published_at IS NULL;
ALTER TABLE ingest_outbox ADD COLUMN IF NOT EXISTS stage TEXT;
ALTER TABLE ingest_outbox ADD COLUMN IF NOT EXISTS discarded_at TIMESTAMPTZ;
ALTER TABLE ingest_outbox ADD COLUMN IF NOT EXISTS discard_reason TEXT;
UPDATE ingest_outbox o SET stage=j.stage FROM ingest_jobs j
 WHERE o.job_id=j.id AND o.stage IS NULL;
CREATE INDEX IF NOT EXISTS ingest_outbox_stage_pending ON ingest_outbox(stage,ready_at,id)
 WHERE published_at IS NULL;
CREATE INDEX IF NOT EXISTS ingest_outbox_stage_ready ON ingest_outbox(stage,ready_at,id)
 WHERE published_at IS NULL AND discarded_at IS NULL;
CREATE TABLE IF NOT EXISTS ingest_host_cooldowns (
 host TEXT PRIMARY KEY,available_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_host_admission (
 host TEXT PRIMARY KEY,last_admitted TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ingest_workers (
 id TEXT PRIMARY KEY,role TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',
 heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ingest_route_profiles (
 source_id BIGINT PRIMARY KEY REFERENCES sites(id),rules JSONB NOT NULL,
 learned_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ingest_document_plans (
 id TEXT PRIMARY KEY,poll_id TEXT NOT NULL REFERENCES ingest_polls(id),
 body BYTEA NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ingest_document_cache (
 source_id BIGINT NOT NULL REFERENCES sites(id),url_hash TEXT NOT NULL,revision TEXT NOT NULL,
 url TEXT NOT NULL,content_hash TEXT NOT NULL,etag TEXT,last_modified TEXT,document BYTEA NOT NULL,
 checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(source_id,url_hash,revision)
);
"""
