-- Estado do buscador-midias no Postgres (banco `pgsql_midias`, o mesmo do corpus `midias_ia_*`).
--
-- Tradução do esquema SQLite (`var/state.db`, `src/buscador_midias/state/store.py`). Prefixo
-- `midias_crawler_` para não se misturar com o corpus. Mudanças em relação ao SQLite:
--
--   * datas: texto ISO 8601 -> timestamptz (sempre UTC na gravação);
--   * hashes em varchar(64) (não char: o preenchimento com espaços quebraria hashes curtos de teste);
--   * `finding` e `created`: 0/1 -> boolean;
--   * `themes` e `rejected_reasons`: texto separado por vírgula -> text[]
--       (carga: string_to_array(NULLIF(coluna, ''), ',')  /  COALESCE(..., '{}'));
--   * `entities`: texto JSON -> jsonb (carga: entities::jsonb);
--   * `urls_seen.rowid` virou `midias_crawler_urls.id` (ordem de chegada, usada na fila de análise);
--   * `kind`, `status`, `basis` etc. ganharam CHECK com os valores que o código grava;
--   * as tabelas da triagem por manchete (`triage_runs`, `url_triage`) NÃO migram: o passo foi
--     removido em 2026-10-16 e nada mais lê essas tabelas;
--   * `corpus_runs.withheld` não migra: a regra de "retidos" deixou de existir.
--
-- Sem chave estrangeira para `midias_ia_articles`: o corpus pode perder linhas (limpeza, teste) e o
-- buscador as refaz; `corpus_writes.article_id` é só a última pista.
--
-- O critério de entrada no corpus (`src/buscador_midias/criteria.py`) é: basis = 'text' E ao menos
-- uma entidade. Os índices parciais de `midias_crawler_analyses` seguem essa regra.

-- ---------------------------------------------------------------------------------------------
-- Rodadas
-- ---------------------------------------------------------------------------------------------

CREATE TABLE midias_crawler_runs (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at           timestamptz NOT NULL,
    finished_at          timestamptz,
    bright_data_requests integer NOT NULL DEFAULT 0,
    bright_data_hosts    integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE midias_crawler_runs IS 'Rodadas de descoberta (uma por execução do discover).';

CREATE TABLE midias_crawler_enrich_runs (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at           timestamptz NOT NULL,
    finished_at          timestamptz,
    model                text NOT NULL,
    prompt_version       text NOT NULL,
    evidence_mode        varchar(16) NOT NULL CHECK (evidence_mode IN ('auto', 'brightdata')),
    urls                 integer NOT NULL DEFAULT 0,
    fetched_ok           integer NOT NULL DEFAULT 0,
    fetched_blocked      integer NOT NULL DEFAULT 0,
    fetched_other        integer NOT NULL DEFAULT 0,
    analyzed             integer NOT NULL DEFAULT 0,
    findings             integer NOT NULL DEFAULT 0,
    evidence_reused      integer NOT NULL DEFAULT 0,
    evidence_brightdata  integer NOT NULL DEFAULT 0,
    evidence_failed      integer NOT NULL DEFAULT 0,
    bright_data_requests integer NOT NULL DEFAULT 0,
    prompt_tokens        integer NOT NULL DEFAULT 0,
    completion_tokens    integer NOT NULL DEFAULT 0,
    cost_usd             double precision,
    retries              integer NOT NULL DEFAULT 0,
    errors               integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE midias_crawler_enrich_runs IS 'Execuções do enriquecimento (texto, análise de contexto, critério e evidência), com consumo e custo.';

CREATE TABLE midias_crawler_corpus_runs (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at       timestamptz NOT NULL,
    finished_at      timestamptz,
    candidates       integer NOT NULL DEFAULT 0,
    created          integer NOT NULL DEFAULT 0,
    existing         integer NOT NULL DEFAULT 0,
    evidence_updated integer NOT NULL DEFAULT 0,
    entities         integer NOT NULL DEFAULT 0,
    keywords         integer NOT NULL DEFAULT 0,
    failed           integer NOT NULL DEFAULT 0,
    rewritten        integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE midias_crawler_corpus_runs IS 'Execuções da gravação no corpus (corpus-sync).';

-- ---------------------------------------------------------------------------------------------
-- Descoberta
-- ---------------------------------------------------------------------------------------------

CREATE TABLE midias_crawler_urls (
    url_hash         varchar(64) PRIMARY KEY,
    id               bigint GENERATED ALWAYS AS IDENTITY,
    url              text NOT NULL,
    site_key         text NOT NULL,
    source_sitemap   text NOT NULL,
    kind             varchar(16) NOT NULL
                     CHECK (kind IN ('new', 'baseline', 'backlog', 'known', 'filtered')),
    first_seen_run   bigint NOT NULL REFERENCES midias_crawler_runs (id),
    first_seen_at    timestamptz NOT NULL,
    lastmod          timestamptz,
    title            text,
    publication_date timestamptz,
    filter_reason    varchar(32)
);
COMMENT ON TABLE midias_crawler_urls IS 'Toda URL já vista (identidade = hash canônico, o mesmo do corpus). A primeira classificação (kind) vale para sempre.';
COMMENT ON COLUMN midias_crawler_urls.id IS 'Ordem de chegada (era o rowid do SQLite); a fila de análise a usa.';
COMMENT ON COLUMN midias_crawler_urls.source_sitemap IS 'Sitemap de origem, ou feed:<fonte> quando veio de um feed.';
CREATE UNIQUE INDEX midias_crawler_urls_id_unique ON midias_crawler_urls (id);
CREATE INDEX midias_crawler_urls_kind_seen ON midias_crawler_urls (kind, first_seen_at);
CREATE INDEX midias_crawler_urls_run ON midias_crawler_urls (first_seen_run);
CREATE INDEX midias_crawler_urls_site ON midias_crawler_urls (site_key);

CREATE TABLE midias_crawler_site_kind_counts (
    site_key text NOT NULL,
    kind     varchar(16) NOT NULL
             CHECK (kind IN ('new', 'baseline', 'backlog', 'known', 'filtered')),
    n        bigint NOT NULL,
    PRIMARY KEY (site_key, kind)
);
COMMENT ON TABLE midias_crawler_site_kind_counts IS 'Contadores por fonte e tipo mantidos a cada gravação (evita varrer midias_crawler_urls no painel).';

CREATE TABLE midias_crawler_sitemaps (
    sitemap_url text PRIMARY KEY,
    site_key    text NOT NULL,
    lastmod     timestamptz,
    fetched_at  timestamptz NOT NULL
);
CREATE INDEX midias_crawler_sitemaps_site ON midias_crawler_sitemaps (site_key);
COMMENT ON TABLE midias_crawler_sitemaps IS 'Último lastmod declarado de cada sitemap lido; sitemap filho inalterado é pulado.';

CREATE TABLE midias_crawler_site_baselines (
    site_key text PRIMARY KEY,
    run_id   bigint NOT NULL REFERENCES midias_crawler_runs (id)
);
COMMENT ON TABLE midias_crawler_site_baselines IS 'Fontes cuja linha de base (primeira leitura, só histórico) já foi feita.';

CREATE TABLE midias_crawler_site_runs (
    run_id           bigint NOT NULL REFERENCES midias_crawler_runs (id),
    site_key         text NOT NULL,
    status           varchar(24) NOT NULL
                     CHECK (status IN ('ok', 'no_sitemap', 'blocked', 'robots_disallowed', 'error')),
    new_urls         integer NOT NULL,
    baseline_urls    integer NOT NULL,
    backlog_urls     integer NOT NULL,
    seen_urls        integer NOT NULL,
    sitemaps_fetched integer NOT NULL,
    sitemaps_skipped integer NOT NULL,
    detail           text NOT NULL,
    known_urls       integer NOT NULL DEFAULT 0,
    sitemaps_failed  integer NOT NULL DEFAULT 0,
    filtered_urls    integer NOT NULL DEFAULT 0,
    source           varchar(16) NOT NULL DEFAULT ''
                     CHECK (source IN ('', 'sitemap', 'feed', 'sitemap_bd')),
    PRIMARY KEY (run_id, site_key)
);
CREATE INDEX midias_crawler_site_runs_site ON midias_crawler_site_runs (site_key, run_id);
COMMENT ON TABLE midias_crawler_site_runs IS 'O que a descoberta de cada fonte produziu em cada rodada; source = de onde vieram as URLs (vazio em falha).';

CREATE TABLE midias_crawler_fallback_hosts (
    host            text PRIMARY KEY,
    first_marked_at timestamptz NOT NULL
);
COMMENT ON TABLE midias_crawler_fallback_hosts IS 'Hosts que já bloquearam o acesso gratuito: o sitemap gratuito não é tentado de novo, o feed vem antes de pagar.';

-- ---------------------------------------------------------------------------------------------
-- Enriquecimento
-- ---------------------------------------------------------------------------------------------

CREATE TABLE midias_crawler_contents (
    url_hash     varchar(64) PRIMARY KEY REFERENCES midias_crawler_urls (url_hash),
    status       varchar(24) NOT NULL
                 CHECK (status IN ('ok', 'blocked', 'not_found', 'robots_disallowed', 'error', 'unusable')),
    http_status  smallint,
    final_url    text,
    title        text,
    description  text,
    lede         text,
    text_chars   integer NOT NULL DEFAULT 0,
    published_at timestamptz,
    language     varchar(32),
    error        text,
    attempts     smallint NOT NULL DEFAULT 0,
    fetched_at   timestamptz NOT NULL
);
COMMENT ON TABLE midias_crawler_contents IS 'Resultado do download direto e da extração do texto de cada URL analisada.';

CREATE TABLE midias_crawler_analyses (
    url_hash       varchar(64) PRIMARY KEY REFERENCES midias_crawler_urls (url_hash),
    finding        boolean NOT NULL,
    themes         text[] NOT NULL DEFAULT '{}',
    entities       jsonb NOT NULL DEFAULT '[]'::jsonb,
    adverse_fact   text NOT NULL,
    summary        text NOT NULL,
    confidence     double precision NOT NULL,
    basis          varchar(16) NOT NULL CHECK (basis IN ('text', 'headline')),
    model          text NOT NULL,
    prompt_version text NOT NULL,
    enrich_run_id  bigint NOT NULL REFERENCES midias_crawler_enrich_runs (id),
    analyzed_at    timestamptz NOT NULL
);
COMMENT ON TABLE midias_crawler_analyses IS 'Análise de contexto (LLM) de cada URL: entidades, temas e fato adverso. Uma análise por URL.';
COMMENT ON COLUMN midias_crawler_analyses.finding IS 'Achado adverso (marca à parte; não é o filtro do corpus).';
COMMENT ON COLUMN midias_crawler_analyses.entities IS 'Lista de {name, type, role}; só nomes que constam no texto analisado.';
CREATE INDEX midias_crawler_analyses_run ON midias_crawler_analyses (enrich_run_id);
CREATE INDEX midias_crawler_analyses_at ON midias_crawler_analyses (analyzed_at);
-- Critério do corpus (criteria.py): texto lido E ao menos uma entidade.
CREATE INDEX midias_crawler_analyses_meets_criteria ON midias_crawler_analyses (analyzed_at, url_hash)
    WHERE basis = 'text' AND entities <> '[]'::jsonb;

CREATE TABLE midias_crawler_evidences (
    url_hash         varchar(64) PRIMARY KEY REFERENCES midias_crawler_urls (url_hash),
    status           varchar(16) NOT NULL CHECK (status IN ('captured', 'failed')),
    provider         varchar(16) CHECK (provider IN ('direct', 'brightdata')),
    path             text,
    sha256           varchar(64),
    size_bytes       bigint NOT NULL DEFAULT 0,
    content_type     text,
    http_status      smallint,
    final_url        text,
    rejected_reasons text[] NOT NULL DEFAULT '{}',
    error            text,
    attempts         smallint NOT NULL DEFAULT 0,
    updated_at       timestamptz NOT NULL
);
COMMENT ON TABLE midias_crawler_evidences IS 'Estado da captura da evidência (HTML guardado) de cada URL que atende ao critério.';
COMMENT ON COLUMN midias_crawler_evidences.rejected_reasons IS 'Por que o HTML do download direto não foi reaproveitado (códigos de extraction/reliability.py).';
CREATE INDEX midias_crawler_evidences_status ON midias_crawler_evidences (status, attempts);

-- ---------------------------------------------------------------------------------------------
-- Corpus
-- ---------------------------------------------------------------------------------------------

CREATE TABLE midias_crawler_corpus_writes (
    url_hash       varchar(64) PRIMARY KEY REFERENCES midias_crawler_urls (url_hash),
    article_id     bigint,
    created        boolean NOT NULL,
    evidence_state varchar(16) NOT NULL,
    written_at     timestamptz NOT NULL,
    corpus_run_id  bigint NOT NULL REFERENCES midias_crawler_corpus_runs (id)
);
COMMENT ON TABLE midias_crawler_corpus_writes IS 'O que já foi gravado em midias_ia_articles e com que estado de evidência; muda o estado, grava de novo.';
COMMENT ON COLUMN midias_crawler_corpus_writes.created IS 'true se o artigo foi criado por este sistema (false: já existia, de qualquer origem).';
CREATE INDEX midias_crawler_corpus_writes_run ON midias_crawler_corpus_writes (corpus_run_id);
