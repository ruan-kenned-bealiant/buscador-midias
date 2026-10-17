-- Leitura de mecanismos para conferir amostras das páginas do inventário.
CREATE TABLE midias_crawler_section_sources (
    site_key text NOT NULL,
    url text NOT NULL,
    verdict text NOT NULL,
    kind text,
    listed_urls jsonb NOT NULL DEFAULT '[]'::jsonb,
    checked_at timestamptz NOT NULL,
    requests integer NOT NULL DEFAULT 0,
    partial boolean NOT NULL DEFAULT false,
    detail text NOT NULL DEFAULT '',
    PRIMARY KEY (site_key, url)
);
COMMENT ON TABLE midias_crawler_section_sources IS
    'Leituras de feeds/sitemaps para comparar amostras das seções, sem inferir cobertura total. Independente das estratégias de produção.';
