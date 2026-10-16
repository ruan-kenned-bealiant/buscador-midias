import csv
from pathlib import Path

import pytest
from test_labeling import build_store  # fixtures do módulo irmão

from buscador_midias.cli import build_parser, main
from buscador_midias.state.store import StateStore


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "estado.db"
    source = build_store(findings=6, no_entity=6)
    source.backup(path)
    return path


def test_parser_dos_rotulos_tem_padroes_razoaveis():
    export = build_parser().parse_args(["labels", "export"])
    score = build_parser().parse_args(["labels", "score"])

    assert (export.per_group, export.seed) == (70, 1)
    assert export.out_dir.name == "labels" and score.out_dir.name == "labels"


def test_export_grava_o_lote_e_diz_o_que_fazer(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    out = tmp_path / "rotulos"

    code = main(["labels", "export", "--db", str(db), "--out-dir", str(out), "--per-group", "3"])

    assert code == 0
    text = capsys.readouterr().out
    assert "rotulagem-01.csv" in text
    assert "veredito" in text and "sim" in text and "corpus" in text
    assert len(list(csv.DictReader((out / "rotulagem-01.csv").open(encoding="utf-8-sig")))) == 6


def test_score_sem_rotulos_explica_o_que_falta(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    out = tmp_path / "rotulos"
    main(["labels", "export", "--db", str(db), "--out-dir", str(out), "--per-group", "3"])
    capsys.readouterr()

    code = main(["labels", "score", "--db", str(db), "--out-dir", str(out)])

    assert code == 0
    text = capsys.readouterr().out
    assert "mandadas ao corpus pelo modelo" in text
    assert "sem rótulos" in text
    assert "n/d" in text


def test_score_sem_exportar_antes_aborta(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    code = main(["labels", "score", "--db", str(db), "--out-dir", str(tmp_path / "vazio")])

    assert code == 2
    assert "export" in capsys.readouterr().err


def test_sem_estado_aborta(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    code = main(
        [
            "labels",
            "export",
            "--db",
            str(tmp_path / "nao-existe.db"),
            "--out-dir",
            str(tmp_path / "o"),
        ]
    )

    assert code == 2
    assert "estado" in capsys.readouterr().err
    assert StateStore  # importado para tipagem dos fixtures


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
    db: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    from http.server import ThreadingHTTPServer

    out = tmp_path / "rotulos"
    main(["labels", "export", "--db", str(db), "--out-dir", str(out), "--per-group", "2"])
    capsys.readouterr()

    def interrupted(self: ThreadingHTTPServer, *_: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", interrupted)

    code = main(["labels", "serve", "--out-dir", str(out), "--port", "0"])

    assert code == 0
    assert "http://127.0.0.1:" in capsys.readouterr().out


def test_serve_com_porta_ocupada_avisa_sem_traceback(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    import socket

    out = tmp_path / "rotulos"
    main(["labels", "export", "--db", str(db), "--out-dir", str(out), "--per-group", "2"])
    capsys.readouterr()
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()

        code = main(
            ["labels", "serve", "--out-dir", str(out), "--port", str(busy.getsockname()[1])]
        )

    assert code == 2
    assert "porta" in capsys.readouterr().err
