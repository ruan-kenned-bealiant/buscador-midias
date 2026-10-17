-- Distingue a origem da candidata da evidência de seu tipo real.
ALTER TABLE midias_crawler_section_pages
    ADD COLUMN page_type text NOT NULL DEFAULT 'unknown'
        CHECK (page_type IN ('unknown', 'section', 'article', 'service', 'taxonomy')),
    ADD COLUMN type_basis text NOT NULL DEFAULT '',
    ADD COLUMN discovery_basis text NOT NULL DEFAULT 'url_shape';

UPDATE midias_crawler_section_pages SET discovery_basis = 'entrypoint' WHERE kind = 'root';
UPDATE midias_crawler_section_pages
    SET page_type = 'taxonomy', type_basis = 'taxonomy_path' WHERE kind = 'taxonomy';

COMMENT ON COLUMN midias_crawler_section_pages.page_type IS
    'Tipo identificado por evidência; unknown continua candidata. Artigos e serviços não expandem a fila de seções.';
COMMENT ON COLUMN midias_crawler_section_pages.discovery_basis IS
    'Como a candidata foi encontrada: entrada, navegação explícita ou formato de URL.';
