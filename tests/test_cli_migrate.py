from pathlib import Path

import pytest

from buscador_midias.cli import build_parser, main
from buscador_midias.state.store import StateStore


def test_parser_do_migrate_exige_o_arquivo():
    args = build_parser().parse_args(["migrate-sqlite", "var/state.db"])

    assert args.sqlite == Path("var/state.db")


def test_migra_e_imprime_quantas_linhas_foram_por_tabela(
    legacy: Path,
    capsys: pytest.CaptureFixture[str],
):
    code = main(["migrate-sqlite", str(legacy)])

    out = capsys.readouterr().out
    assert code == 0
    assert "urls_seen" in out and "3" in out
    assert StateStore(read_only=True).count_urls() == 3


def test_segunda_migracao_para_o_mesmo_destino_e_recusada(
    legacy: Path,
    capsys: pytest.CaptureFixture[str],
):
    assert main(["migrate-sqlite", str(legacy)]) == 0
    capsys.readouterr()

    code = main(["migrate-sqlite", str(legacy)])

    assert code == 2
    assert "já tem dados" in capsys.readouterr().err


def test_arquivo_inexistente_aborta(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    code = main(["migrate-sqlite", str(tmp_path / "nao-existe.db")])

    assert code == 2
    assert "não encontrado" in capsys.readouterr().err
