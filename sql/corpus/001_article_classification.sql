-- Portável para futura migration do PowerDD; não reclassifica o histórico.
ALTER TABLE public.midias_ia_articles
 ADD COLUMN IF NOT EXISTS content_category VARCHAR(32),
 ADD COLUMN IF NOT EXISTS source_country_method VARCHAR(255);
