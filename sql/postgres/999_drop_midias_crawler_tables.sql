-- Desfaz 001_create_midias_crawler_tables.sql e 002_create_midias_crawler_source_strategies.sql
-- (ordem inversa das chaves estrangeiras).
DROP TABLE IF EXISTS midias_crawler_source_strategies;
DROP TABLE IF EXISTS midias_crawler_corpus_writes;
DROP TABLE IF EXISTS midias_crawler_evidences;
DROP TABLE IF EXISTS midias_crawler_analyses;
DROP TABLE IF EXISTS midias_crawler_contents;
DROP TABLE IF EXISTS midias_crawler_fallback_hosts;
DROP TABLE IF EXISTS midias_crawler_site_runs;
DROP TABLE IF EXISTS midias_crawler_site_baselines;
DROP TABLE IF EXISTS midias_crawler_sitemaps;
DROP TABLE IF EXISTS midias_crawler_site_kind_counts;
DROP TABLE IF EXISTS midias_crawler_urls;
DROP TABLE IF EXISTS midias_crawler_corpus_runs;
DROP TABLE IF EXISTS midias_crawler_enrich_runs;
DROP TABLE IF EXISTS midias_crawler_runs;
