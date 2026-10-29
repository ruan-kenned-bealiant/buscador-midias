"""O comando ``dashboard``: parser e subida do servidor sem bloquear."""

from __future__ import annotations

import pytest

from buscador_midias.cli import build_parser, main


def test_the_dashboard_listens_only_on_this_machine_by_default():
    args = build_parser().parse_args(["dashboard"])
    assert (args.host, args.port) == ("127.0.0.1", 8765)


def test_a_subcommand_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_the_dashboard_starts_and_prints_its_address(capsys):
    started: list[tuple[str, int]] = []

    code = main(
        ["dashboard", "--port", "0"], serve=lambda server: started.append(server.server_address[:2])
    )

    assert code == 0 and started and started[0][0] == "127.0.0.1"
    assert "painel em http://127.0.0.1:" in capsys.readouterr().out
