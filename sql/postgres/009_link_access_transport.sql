-- Transporte comprovado por link; independente do mecanismo RSS/sitemap.
-- Migração aditiva para o gerenciador local. Portar junto com sites/links ao PowerDD.
ALTER TABLE IF EXISTS midias_manager.links
    ADD COLUMN IF NOT EXISTS access_transport TEXT NOT NULL DEFAULT 'direct';
