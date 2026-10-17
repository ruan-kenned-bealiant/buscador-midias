-- Origem da descoberta pela união de principal/reserva e complementos gratuitos.
-- Amplia somente o CHECK do estado do crawler; não altera linhas ou tabelas do corpus.
ALTER TABLE midias_crawler_site_runs
    DROP CONSTRAINT IF EXISTS midias_crawler_site_runs_source_check;
ALTER TABLE midias_crawler_site_runs
    ADD CONSTRAINT midias_crawler_site_runs_source_check
    CHECK (source IN ('', 'sitemap', 'feed', 'sitemap_bd', 'combined'));
