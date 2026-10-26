-- Campos de classificação novos; análises anteriores continuam sem classificação conhecida.
ALTER TABLE midias_crawler_analyses
 ADD COLUMN IF NOT EXISTS content_category VARCHAR(32),
 ADD COLUMN IF NOT EXISTS article_country VARCHAR(2),
 ADD COLUMN IF NOT EXISTS article_country_evidence TEXT;
