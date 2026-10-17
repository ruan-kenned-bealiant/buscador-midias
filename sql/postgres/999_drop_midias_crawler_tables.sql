-- Desfaz os arquivos 001 a 006 (ordem inversa das chaves estrangeiras; 006 altera a tabela 004).
DROP TABLE IF EXISTS midias_crawler_section_sources;
DROP TABLE IF EXISTS midias_crawler_section_pages;
DROP TABLE IF EXISTS midias_crawler_source_probe_state;
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
