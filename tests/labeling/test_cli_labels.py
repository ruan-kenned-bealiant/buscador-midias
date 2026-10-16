import csv
from pathlib import Path

import pytest
from test_labeling import build_store  # fixtures do módulo irmão

from buscador_midias.cli import build_parser, main


@pytest.fixture
def db() -> None:
    build_store(findings=6, no_entity=6)  # grava no schema do teste


def test_parser_dos_rotulos_tem_padroes_razoaveis():
    export = build_parser().parse_args(["labels", "export"])
    score = build_parser().parse_args(["labels", "score"])

    assert (export.per_group, export.seed) == (70, 1)
    assert export.out_dir.name == "labels" and score.out_dir.name == "labels"


def test_export_grava_o_lote_e_diz_o_que_fazer(
    db: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    out = tmp_path / "rotulos"

    code = main(["labels", "export", "--out-dir", str(out), "--per-group", "3"])

    assert code == 0
    text = capsys.readouterr().out
    assert "rotulagem-01.csv" in text
    assert "veredito" in text and "sim" in text and "corpus" in text
    assert len(list(csv.DictReader((out / "rotulagem-01.csv").open(encoding="utf-8-sig")))) == 6


def test_score_sem_rotulos_explica_o_que_falta(
    db: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    out = tmp_path / "rotulos"
    main(["labels", "export", "--out-dir", str(out), "--per-group", "3"])
    capsys.readouterr()

    code = main(["labels", "score", "--out-dir", str(out)])

    assert code == 0
    text = capsys.readouterr().out
    assert "mandadas ao corpus pelo modelo" in text
    assert "sem rótulos" in text
    assert "n/d" in text


def test_score_sem_exportar_antes_aborta(
    db: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    code = main(["labels", "score", "--out-dir", str(tmp_path / "vazio")])

    assert code == 2
    assert "export" in capsys.readouterr().err


def test_sem_conexao_com_o_estado_aborta_sem_vazar_a_senha(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DB_MIDIAS_IA_PASSWORD", "segredo-123")
    monkeypatch.setenv("DB_MIDIAS_IA_PORT", "1")  # nada escuta nessa porta

    code = main(["labels", "export", "--out-dir", str(tmp_path / "o")])

    err = capsys.readouterr().err
    assert code == 2
    assert "estado" in err
    assert "segredo-123" not in err


def test_parser_do_serve_tem_padroes_seguros():
    args = build_parser().parse_args(["labels", "serve"])

    assert args.host == "127.0.0.1"
    assert args.port == 8766
    assert args.out_dir.name == "labels"


def test_serve_nao_precisa_do_estado_mas_exige_lote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    code = main(["labels", "serve", "--out-dir", str(tmp_path / "vazio"), "--port", "0"])

    assert code == 2
    assert "export" in capsys.readouterr().err


def test_serve_sobe_mostra_o_endereco_e_encerra_com_ctrl_c(
    db: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    from http.server import ThreadingHTTPServer

    out = tmp_path / "rotulos"
    main(["labels", "export", "--out-dir", str(out), "--per-group", "2"])
    capsys.readouterr()

    def interrupted(self: ThreadingHTTPServer, *_: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", interrupted)

    code = main(["labels", "serve", "--out-dir", str(out), "--port", "0"])

    assert code == 0
    assert "http://127.0.0.1:" in capsys.readouterr().out


def test_serve_com_porta_ocupada_avisa_sem_traceback(
    db: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    import socket

    out = tmp_path / "rotulos"
    main(["labels", "export", "--out-dir", str(out), "--per-group", "2"])
    capsys.readouterr()
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()

        code = main(
            ["labels", "serve", "--out-dir", str(out), "--port", str(busy.getsockname()[1])]
        )

    assert code == 2
    assert "porta" in capsys.readouterr().err
