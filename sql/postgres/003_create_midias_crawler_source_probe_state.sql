-- Quando cada fonte volta a ser sondada (banco `pgsql_midias`, junto das demais midias_crawler_*).
--
-- Controle de saúde da estratégia de descoberta (`src/buscador_midias/strategy/health.py`): uma linha
-- por fonte já sondada, com o resultado da última sondagem, a espera crescente entre reavaliações
-- por saúde e a idade em que a estratégia é reavaliada de qualquer jeito (ou, para uma fonte sem
-- estratégia, quando tentar de novo). Complementa `midias_crawler_source_strategies`, que só tem
-- linhas de fontes em que a sondagem achou um principal.

CREATE TABLE midias_crawler_source_probe_state (
    site_key       text PRIMARY KEY,
    probed_at      timestamptz NOT NULL,
    found          boolean NOT NULL,
    streak         smallint NOT NULL DEFAULT 0,
    cooldown_until timestamptz NOT NULL,
    refresh_at     timestamptz NOT NULL,
    detail         text NOT NULL DEFAULT '',
    reason         varchar(16) NOT NULL DEFAULT ''
                   CHECK (reason IN ('', 'never_probed', 'retry', 'refresh', 'failing', 'silent',
                                     'shrunk'))
);
COMMENT ON TABLE midias_crawler_source_probe_state IS 'Estado da reavaliação da estratégia de cada fonte: última sondagem, espera crescente e idade máxima.';
COMMENT ON COLUMN midias_crawler_source_probe_state.found IS 'Se a última sondagem achou um principal.';
COMMENT ON COLUMN midias_crawler_source_probe_state.streak IS 'Sondagens seguidas sem melhora (por saúde sem mudar a escolha, ou sem achar nada); dobra a espera.';
COMMENT ON COLUMN midias_crawler_source_probe_state.cooldown_until IS 'Antes disso, falha, silêncio ou encolhimento não pedem reavaliação.';
COMMENT ON COLUMN midias_crawler_source_probe_state.refresh_at IS 'Quando a estratégia é reavaliada por idade (sem estratégia: quando tentar de novo).';
COMMENT ON COLUMN midias_crawler_source_probe_state.detail IS 'Por que a última sondagem não achou estratégia (ex.: blocked 13); vazio quando achou.';
COMMENT ON COLUMN midias_crawler_source_probe_state.reason IS 'Por que a última sondagem aconteceu (vazio: manual).';
