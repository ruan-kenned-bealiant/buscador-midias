from pathlib import Path

import pytest

from buscador_midias.sites.loader import load_sites

HEADER = "region,name,url,country,country_code,language,source_type\n"


def write_csv(tmp_path: Path, body: str, header: str = HEADER) -> Path:
    path = tmp_path / "sites.csv"
    path.write_text(header + body, encoding="utf-8")
    return path


def test_carrega_sites_do_csv(tmp_path: Path):
    path = write_csv(
        tmp_path,
        "Brasil,Folha,https://www.folha.uol.com.br,Brasil,BR,Português,Jornal\n"
        'Brasil,"O Globo, online",https://oglobo.globo.com,Brasil,BR,Português,Jornal\n',
    )

    result = load_sites(path)

    assert [s.name for s in result.sites] == ["Folha", "O Globo, online"]
    assert result.sites[0].host == "folha.uol.com.br"
    assert result.sites[0].country_code == "BR"
    assert result.duplicates == ()


def test_deduplica_por_chave_mantendo_a_primeira(tmp_path: Path):
    path = write_csv(
        tmp_path,
        "Brasil,Estadão A,https://www.estadao.com.br,Brasil,BR,Português,Jornal\n"
        "Global,Estadão B,https://www.estadao.com.br/,Brasil,BR,Português,Agência\n"
        "Brasil,Outro,https://outro.com.br,Brasil,BR,Português,Jornal\n",
    )

    result = load_sites(path)

    assert [s.name for s in result.sites] == ["Estadão A", "Outro"]
    assert [s.name for s in result.duplicates] == ["Estadão B"]


def test_www_e_sem_www_sao_o_mesmo_site(tmp_path: Path):
    path = write_csv(
        tmp_path,
        "A,X,https://www.exemplo.com,Brasil,BR,Português,Jornal\n"
        "B,Y,https://exemplo.com,Brasil,BR,Português,Jornal\n",
    )

    result = load_sites(path)

    assert len(result.sites) == 1
    assert len(result.duplicates) == 1


def test_secao_e_dominio_inteiro_sao_sites_distintos(tmp_path: Path):
    path = write_csv(
        tmp_path,
        "A,G1,https://g1.globo.com,Brasil,BR,Português,Portal\n"
        "A,GloboNews,https://g1.globo.com/globonews/,Brasil,BR,Português,TV\n",
    )

    result = load_sites(path)

    assert [s.key for s in result.sites] == ["g1.globo.com", "g1.globo.com/globonews"]
    assert result.duplicates == ()


def test_coluna_ausente_levanta_erro_claro(tmp_path: Path):
    path = write_csv(tmp_path, "A,B\n", header="region,name\n")

    with pytest.raises(ValueError, match="colunas ausentes"):
        load_sites(path)


def test_url_invalida_informa_a_linha(tmp_path: Path):
    path = write_csv(tmp_path, "A,Quebrada,sem-esquema,Brasil,BR,Português,Jornal\n")

    with pytest.raises(ValueError, match="linha 2"):
        load_sites(path)
