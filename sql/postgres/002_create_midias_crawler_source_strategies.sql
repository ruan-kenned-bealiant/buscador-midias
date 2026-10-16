-- Estratégia de descoberta de cada fonte (banco `pgsql_midias`, junto das demais midias_crawler_*).
--
-- Resultado da sondagem de `probe-sources` (`src/buscador_midias/strategy/`): para cada fonte, todos
-- os endereços candidatos que foram baixados e medidos (sitemap de notícias, sitemap comum, feed),
-- o que se concluiu de cada um e quais foram escolhidos (um principal e, de outro tipo de documento,
-- uma reserva, mais complementos que acrescentam o que eles não listam). A sondagem de uma fonte
-- substitui por inteiro as linhas dela.
--
-- Sem chave estrangeira: a sondagem é independente das rodadas de descoberta.

CREATE TABLE midias_crawler_source_strategies (
    site_key     text NOT NULL,
    url          text NOT NULL,
    origin       varchar(16) NOT NULL
                 CHECK (origin IN ('hint', 'robots', 'common_path', 'home_link')),
    verdict      varchar(16) NOT NULL
                 CHECK (verdict IN ('fresh', 'undated', 'slow', 'stale', 'archive', 'empty',
                                    'invalid', 'blocked', 'disallowed', 'unavailable')),
    kind         varchar(16) CHECK (kind IN ('news_sitemap', 'sitemap', 'feed')),
    role         varchar(10) CHECK (role IN ('primary', 'backup', 'complement')),
    score        double precision NOT NULL DEFAULT 0,
    coverage     real,
    union_recent integer NOT NULL DEFAULT 0,
    cover_share  real,
    section_count smallint NOT NULL DEFAULT 0,
    top_sections jsonb NOT NULL DEFAULT '[]'::jsonb,
    items        integer NOT NULL DEFAULT 0,
    dated_items  integer NOT NULL DEFAULT 0,
    recent_items integer NOT NULL DEFAULT 0,
    titled_items integer NOT NULL DEFAULT 0,
    newest_at    timestamptz,
    size_bytes   bigint NOT NULL DEFAULT 0,
    requests     smallint NOT NULL DEFAULT 0,
    detail       text NOT NULL DEFAULT '',
    probed_at    timestamptz NOT NULL,
    PRIMARY KEY (site_key, url)
);
COMMENT ON TABLE midias_crawler_source_strategies IS 'Candidatos sondados para descobrir as matérias de cada fonte, com o veredito, a nota e o papel (principal/reserva) dos escolhidos.';
COMMENT ON COLUMN midias_crawler_source_strategies.url IS 'Endereço de entrada: pode ser um índice de sitemaps (a medida é a dos melhores filhos dele).';
COMMENT ON COLUMN midias_crawler_source_strategies.recent_items IS 'Matérias publicadas nas últimas 48 h.';
COMMENT ON COLUMN midias_crawler_source_strategies.coverage IS 'Fração das matérias recentes de todos os candidatos frescos que este lista.';
COMMENT ON COLUMN midias_crawler_source_strategies.union_recent IS 'Matérias recentes distintas listadas por todos os candidatos frescos juntos (o site todo que dá para ver); do site, repetido por linha.';
COMMENT ON COLUMN midias_crawler_source_strategies.cover_share IS 'Fração dessa união coberta por principal, reserva e complementos juntos; do site, repetido por linha.';
COMMENT ON COLUMN midias_crawler_source_strategies.section_count IS 'Seções do site (primeiro segmento do caminho) entre as matérias recentes: quanto mais, mais o candidato cobre o site inteiro.';
COMMENT ON COLUMN midias_crawler_source_strategies.top_sections IS 'As seções mais frequentes: lista de [seção, matérias].';
-- No máximo um principal e uma reserva por fonte.
CREATE UNIQUE INDEX midias_crawler_source_strategies_primary
    ON midias_crawler_source_strategies (site_key) WHERE role = 'primary';
CREATE UNIQUE INDEX midias_crawler_source_strategies_backup
    ON midias_crawler_source_strategies (site_key) WHERE role = 'backup';
