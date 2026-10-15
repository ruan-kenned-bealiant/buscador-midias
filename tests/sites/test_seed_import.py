import pytest

from buscador_midias.sites.seed_import import parse_trusted_sources_sql

SEED = """-- comentário
TRUNCATE TABLE midias_ia_trusted_sources RESTART IDENTITY CASCADE;

INSERT INTO midias_ia_trusted_sources ("region", "name") VALUES
('Brasil', 'Folha de S.Paulo', 'https://www.folha.uol.com.br', 'folha.uol.com.br', 'Brasil', 'BR', 'Nacional / São Paulo (SP)', 'Jornal', 'Português', 'Centro', 'Trust Project', 'Fundado em 1921', true, '/x.xlsx', '2026-08-31 18:21:30+00', '2026-08-31 18:21:30+00', '2026-08-31 18:21:30+00'),
('Europa', 'L''Équipe', 'https://www.lequipe.fr', 'lequipe.fr', 'França', 'FR', NULL, 'Jornal', 'Francês', 'Centro', 'Não avaliado', 'Nota com ''aspas''', true, '/x.xlsx', '2026-08-31 18:21:30+00', '2026-08-31 18:21:30+00', '2026-08-31 18:21:30+00');
"""


def test_extrai_campos_relevantes_de_cada_linha():
    rows = parse_trusted_sources_sql(SEED)

    assert rows[0] == {
        "region": "Brasil",
        "name": "Folha de S.Paulo",
        "url": "https://www.folha.uol.com.br",
        "country": "Brasil",
        "country_code": "BR",
        "language": "Português",
        "source_type": "Jornal",
    }


def test_desfaz_escape_de_aspas_e_aceita_sede_nula():
    rows = parse_trusted_sources_sql(SEED)

    assert rows[1]["name"] == "L'Équipe"
    assert rows[1]["country_code"] == "FR"
    assert len(rows) == 2


def test_falha_alto_quando_alguma_linha_nao_e_reconhecida():
    quebrada = SEED + "('Brasil', 'Linha fora do formato');\n"

    with pytest.raises(ValueError, match="2 linhas.*3"):
        parse_trusted_sources_sql(quebrada)
