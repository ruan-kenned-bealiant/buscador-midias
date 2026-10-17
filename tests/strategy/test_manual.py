"""Regras manuais por fonte: excluir um endereço ou forçar outro."""

from pathlib import Path

import pytest
from tests.strategy.helpers import make_site

from buscador_midias.strategy.manual import ManualRules, is_excluded, load_manual_rules

HEADER = "dominio,endereco,acao,motivo\n"


def write(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "manuais.csv"
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_separa_exclusoes_e_forcados_da_fonte(tmp_path: Path):
    rules = load_manual_rules(
        write(
            tmp_path,
            "exemplo.com,https://exemplo.com/tag/*,excluir,só tags",
            "exemplo.com,https://exemplo.com/feed/geral,forçar,feed bom",
            "outro.com,https://outro.com/x,excluir,",
        )
    )

    mine = rules.for_site(make_site("https://exemplo.com"))

    assert mine == ManualRules(
        exclude=("https://exemplo.com/tag/*",), force=("https://exemplo.com/feed/geral",)
    )
    assert rules.for_site(make_site("https://sem-regra.com")) == ManualRules((), ())


def test_aceita_forcar_sem_acento_e_maiusculas_e_colunas_alinhadas(tmp_path: Path):
    path = tmp_path / "manuais.csv"
    path.write_text(
        "dominio      , endereco                , acao   , motivo\n"
        "Exemplo.com/ , https://exemplo.com/a   , FORCAR ,\n",
        encoding="utf-8",
    )

    mine = load_manual_rules(path).for_site(make_site("https://exemplo.com"))

    assert mine.force == ("https://exemplo.com/a",)


def test_secao_tem_regras_proprias_pelo_prefixo(tmp_path: Path):
    rules = load_manual_rules(write(tmp_path, "exemplo.com/ac/acre,https://exemplo.com/f,forçar,"))

    assert rules.for_site(make_site("https://exemplo.com/ac/acre")).force == (
        "https://exemplo.com/f",
    )
    assert rules.for_site(make_site("https://exemplo.com")).force == ()


def test_arquivo_inexistente_nao_tem_regras(tmp_path: Path):
    assert load_manual_rules(tmp_path / "nao-existe.csv").for_site(make_site()) == ManualRules(
        (), ()
    )


def test_acao_desconhecida_e_erro_com_a_linha(tmp_path: Path):
    path = write(tmp_path, "exemplo.com,https://exemplo.com/a,apagar,")

    with pytest.raises(ValueError, match="linha 2.*apagar"):
        load_manual_rules(path)


def test_endereco_de_outro_dominio_e_descartado(tmp_path: Path):
    rules = load_manual_rules(write(tmp_path, "exemplo.com,https://malicioso.com/x,forçar,"))

    assert rules.for_site(make_site("https://exemplo.com")).force == ()


def test_is_excluded_vale_para_o_endereco_exato_e_para_o_prefixo_com_asterisco():
    rules = ("https://a.com/x.xml", "https://a.com/tag/*")

    assert is_excluded("https://a.com/x.xml", rules)
    assert is_excluded("https://a.com/tag/qualquer/coisa", rules)
    assert not is_excluded("https://a.com/x.xml?p=2", rules)
    assert not is_excluded("https://a.com/outro", rules)


def test_byte_invalido_no_csv_manual_tambem_e_tolerado(tmp_path: Path):
    path = tmp_path / "manuais.csv"
    path.write_bytes(
        b"dominio,endereco,acao,motivo\nexemplo.com,https://exemplo.com/a,excluir,sem\xa0motivo\n"
    )

    assert load_manual_rules(path).for_site(make_site("https://exemplo.com")).exclude == (
        "https://exemplo.com/a",
    )
