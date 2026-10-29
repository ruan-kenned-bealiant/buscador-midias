"""Comando ``python -m buscador_midias db``: instalar, migrar e conferir o cadastro."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import pytest
from psycopg import sql

from buscador_midias.db.cli import main
from buscador_midias.db.connection import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.fixture
def target() -> Iterator[str]:
    name = "bm_test_cli_" + uuid.uuid4().hex[:12]
    yield name
    with connect() as db:
        db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def test_install_creates_tables_and_settings_and_is_repeatable(target, capsys):
    assert main(["install", "--schema", target]) == 0
    assert main(["install", "--schema", target]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["tables"] >= 6 and out["settings"] == 3


def test_status_reports_counts_of_the_target(target, capsys):
    main(["install", "--schema", target])
    capsys.readouterr()
    assert main(["status", "--schema", target]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sites"] == 0 and out["links"] == 0 and out["mechanisms"] == 6


def test_unify_sites_reports_nothing_to_do_on_a_fresh_install(target, capsys):
    main(["install", "--schema", target])
    capsys.readouterr()
    assert main(["unify-sites", "--schema", target]) == 0
    assert json.loads(capsys.readouterr().out)["migrated"] is False
