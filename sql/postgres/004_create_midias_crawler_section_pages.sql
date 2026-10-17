-- Inventário estrutural retomável. HTTP ok não é validação de cobertura de notícias.
CREATE TABLE midias_crawler_section_pages (
    id bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    site_key text NOT NULL,
    url text NOT NULL,
    found_on text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('root', 'section', 'taxonomy')),
    status text NOT NULL CHECK (status IN ('pending', 'ok', 'blocked', 'error',
                                           'disallowed', 'invalid', 'excluded')),
    title text NOT NULL DEFAULT '',
    article_urls jsonb NOT NULL DEFAULT '[]'::jsonb,
    feeds jsonb NOT NULL DEFAULT '[]'::jsonb,
    feed_indexes jsonb NOT NULL DEFAULT '[]'::jsonb,
    detail text NOT NULL DEFAULT '',
    attempts integer NOT NULL DEFAULT 0,
    checked_at timestamptz,
    PRIMARY KEY (site_key, url)
);
COMMENT ON TABLE midias_crawler_section_pages IS
    'Páginas de entrada e candidatas a seção, inclusive fila não visitada e falhas. Feeds e links ainda não comprovam cobertura.';
