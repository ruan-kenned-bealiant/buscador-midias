"""``python -m buscador_midias`` encaminha cada família de subcomandos ao módulo certo."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("family", "expected"),
    [("pipeline", "serve"), ("db", "install"), ("ingest", "serve")],
)
def test_each_family_has_its_own_help(family: str, expected: str):
    done = subprocess.run(
        [sys.executable, "-m", "buscador_midias", family, "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0
    assert expected in done.stdout
