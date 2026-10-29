"""DDL aditivo da ingestão, separado do corpus e das rodadas históricas."""

STAGES = ("discovery", "dedup", "fetch", "context", "evidence", "persist")
CONTENT_FILTER_MODES = ("off", "shadow", "enforce")
LASTMOD_FILTER_MODES = CONTENT_FILTER_MODES
URL_FILTER_MODES = CONTENT_FILTER_MODES
HEAD_FILTER_MODES = CONTENT_FILTER_MODES
# Notícia e opinião nunca são bloqueadas: o restante é rotina sem interesse para o corpus.
BLOCKABLE_CATEGORIES = ("weather", "cooking", "sports", "entertainment", "lifestyle", "horoscope")
# Versão do DDL. Aumente ao alterar ``DDL``: o esquema só é reaplicado quando a versão gravada
# é menor, para que reinícios e ferramentas não disputem locks com o serviço em execução.
SCHEMA_VERSION = 1
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
    lastmod_filter="shadow",
    url_filter="shadow",
    head_filter="shadow",
    host_concurrency=1,
    # Teto de vagas por host com fila longa (ver admission.host_limit); 1 desliga.
    host_concurrency_max=1,
    # Fração de links lidos por inteiro para auditar o corte pelo topo (ver flow.audit_sampled).
    head_audit_rate=0.0,
    # "enforce": rodadas agendadas pulam links frios (ver cold_links); "off" lê todos.
    adaptive_polling="off",
    # A primeira rodada do dia lê também o dia anterior inteiro (o que saiu depois da última).
    close_previous_day=False,
    # Passadas totais por notícia que falha: 1 = nunca tenta de novo; 3 = até duas novas tentativas
    # em rodadas seguintes (só para falhas que podem passar; ver flow.PERMANENT_FAILURE).
    failed_retry_limit=1,
    workers=dict(discovery=2, dedup=1, fetch=2, context=1, evidence=1, persist=1),
)
# Índices que as telas usam para contar por rodada. Em bases já existentes são criados por
# ``IngestStore.ensure_report_indexes`` (CONCURRENTLY, sem bloquear a escrita); reaplicar o DDL
# inteiro com o serviço rodando já causou deadlock, então ``SCHEMA_VERSION`` não é alterado.
REPORT_INDEXES = (
    ("ingest_job_run", "ingest_jobs", ("run_id", "stage", "state")),
    # Página mais recente de tarefas de uma rodada: sai já ordenada, sem ordenar a rodada toda.
    ("ingest_job_run_created", "ingest_jobs", ("run_id", "created_at")),
    ("ingest_occurrence_run", "ingest_occurrences", ("run_id", "hash")),
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
ALTER TABLE ingest_outbox ADD COLUMN IF NOT EXISTS host TEXT;
CREATE OR REPLACE FUNCTION ingest_url_host(url TEXT) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $fn$
 SELECT rtrim(trim(both '[]' FROM regexp_replace(split_part(lower(COALESCE(url,'')),'/',3),
 ':[0-9]+$','')),'.')
$fn$;
CREATE OR REPLACE FUNCTION ingest_outbox_fill_host() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
 SELECT ingest_url_host(COALESCE(a.url,j.payload->>'url')) INTO NEW.host
 FROM ingest_jobs j LEFT JOIN ingest_articles a ON a.hash=j.hash WHERE j.id=NEW.job_id;
 RETURN NEW;
END
$fn$;
DO $b$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='ingest_outbox_host'
  AND tgrelid='ingest_outbox'::regclass) THEN
  CREATE TRIGGER ingest_outbox_host BEFORE INSERT ON ingest_outbox
  FOR EACH ROW WHEN (NEW.host IS NULL) EXECUTE FUNCTION ingest_outbox_fill_host();
 END IF;
END $b$;
UPDATE ingest_outbox o SET host=ingest_url_host(COALESCE(a.url,j.payload->>'url'))
 FROM ingest_jobs j LEFT JOIN ingest_articles a ON a.hash=j.hash
 WHERE o.job_id=j.id AND o.host IS NULL AND o.published_at IS NULL;
CREATE INDEX IF NOT EXISTS ingest_outbox_host_ready ON ingest_outbox(stage,host,ready_at,id)
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
CREATE TABLE IF NOT EXISTS ingest_schema (
 version INT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
DDL += "".join(
    f"CREATE INDEX IF NOT EXISTS {name} ON {table}({','.join(columns)});\n"
    for name, table, columns in REPORT_INDEXES
)

# Uma linha por rodada com os números que o painel mostra. Em bases já existentes é criada por
# ``IngestStore.ensure_report_indexes`` (mesmo motivo: não reaplicar o DDL com o serviço no ar).
STATS_TABLE = "ingest_run_stats"
STATS_DDL = (
    "CREATE TABLE IF NOT EXISTS ingest_run_stats ("
    "run_id TEXT PRIMARY KEY REFERENCES ingest_runs(id),final BOOLEAN NOT NULL,"
    "computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),payload JSONB NOT NULL)"
)
DDL += STATS_DDL + ";\n"
