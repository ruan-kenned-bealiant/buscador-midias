"""Migração aditiva da verificação periódica de saúde no PostgreSQL."""

DDL = """
CREATE TABLE IF NOT EXISTS health_settings (
 kind TEXT PRIMARY KEY CHECK(kind IN ('active','recovery')),
 enabled BOOLEAN NOT NULL DEFAULT true, interval_minutes INTEGER NOT NULL DEFAULT 1440
 CHECK(interval_minutes BETWEEN 1 AND 525600), workers INTEGER NOT NULL DEFAULT 8
 CHECK(workers BETWEEN 1 AND 32), max_attempts INTEGER NOT NULL DEFAULT 2
 CHECK(max_attempts BETWEEN 1 AND 3), version INTEGER NOT NULL DEFAULT 1,
 next_run_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE health_settings ADD COLUMN IF NOT EXISTS bright_data BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE health_settings ADD COLUMN IF NOT EXISTS max_bright_requests INTEGER NOT NULL DEFAULT 8
 CHECK(max_bright_requests BETWEEN 1 AND 48);
ALTER TABLE health_settings ADD COLUMN IF NOT EXISTS activate_recovered
 BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE health_settings ADD COLUMN IF NOT EXISTS schedule_hours INTEGER[]
 NOT NULL DEFAULT '{}';
ALTER TABLE health_settings ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'full'
 CHECK(mode IN ('full','probe'));
ALTER TABLE health_settings ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'errors'
 CHECK(scope IN ('errors','inactive'));
CREATE TABLE IF NOT EXISTS health_runs (
 id TEXT PRIMARY KEY,kind TEXT NOT NULL REFERENCES health_settings(kind),
 trigger TEXT NOT NULL CHECK(trigger IN ('manual','scheduled')),
 status TEXT NOT NULL CHECK(status IN ('queued','running','cancelling','completed','cancelled')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ,
 finished_at TIMESTAMPTZ, heartbeat_at TIMESTAMPTZ, owner TEXT, configuration JSONB NOT NULL,
 total INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS health_one_open_kind ON health_runs(kind)
 WHERE status IN ('queued','running','cancelling');
CREATE TABLE IF NOT EXISTS health_results (
 run_id TEXT NOT NULL REFERENCES health_runs(id),link_id BIGINT NOT NULL REFERENCES links(id),
 source_id BIGINT NOT NULL REFERENCES sites(id),host TEXT NOT NULL,snapshot JSONB NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN
 ('pending','running','completed','error','skipped','cancelled')),
 category TEXT,attempts INTEGER NOT NULL DEFAULT 0,result JSONB,
 detail TEXT NOT NULL DEFAULT '', started_at TIMESTAMPTZ,finished_at TIMESTAMPTZ,
 PRIMARY KEY(run_id,link_id)
);
ALTER TABLE health_results ADD COLUMN IF NOT EXISTS activated BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS health_queue ON health_results(run_id,state,host,link_id);
-- Cobre as contagens do histórico sem ler as linhas largas (snapshot em JSON).
CREATE INDEX IF NOT EXISTS health_results_summary ON health_results(run_id,state,category)
 INCLUDE (activated);
CREATE TABLE IF NOT EXISTS health_attempts (
 run_id TEXT NOT NULL,link_id BIGINT NOT NULL,attempt INTEGER NOT NULL,
 category TEXT NOT NULL,result JSONB NOT NULL,observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,link_id,attempt),
 FOREIGN KEY(run_id,link_id) REFERENCES health_results(run_id,link_id)
);
CREATE INDEX IF NOT EXISTS health_event_provenance ON events(entity,entity_id,id DESC);
CREATE TABLE IF NOT EXISTS link_health (
 link_id BIGINT PRIMARY KEY REFERENCES links(id),run_id TEXT NOT NULL,
 category TEXT NOT NULL,checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 link_version INTEGER NOT NULL,source_version INTEGER NOT NULL,model_version INTEGER NOT NULL,
 FOREIGN KEY(run_id,link_id) REFERENCES health_results(run_id,link_id)
);
ALTER TABLE link_health ADD COLUMN IF NOT EXISTS failures INTEGER NOT NULL DEFAULT 0;
"""
