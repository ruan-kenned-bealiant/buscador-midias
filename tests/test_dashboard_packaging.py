"""Tudo o que o painel serve precisa ir no pacote, ou a página some numa instalação não editável."""

from __future__ import annotations

import tomllib
from pathlib import Path

from buscador_midias.dashboard import server

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "src" / "buscador_midias" / "dashboard"


def test_every_file_the_panel_serves_is_declared_as_package_data():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packaged = set(project["tool"]["setuptools"]["package-data"]["buscador_midias.dashboard"])
    served = {*server._PAGES.values(), *server._SCRIPTS.values(), "app.css", "evidence.html"}

    assert served <= packaged, sorted(served - packaged)
    assert all((DASHBOARD / name).exists() for name in packaged)
