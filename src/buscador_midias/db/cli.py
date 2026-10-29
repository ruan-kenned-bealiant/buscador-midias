"""Comando ``python -m buscador_midias db``: instala, migra e confere o cadastro."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from buscador_midias.db.catalog import CatalogRepository
from buscador_midias.db.connection import DatabaseConfigError, configured_schema, connect
from buscador_midias.db.schema import CATALOG_TABLES
from buscador_midias.db.settings import SettingsRepository
from buscador_midias.db.unify import unify_sites


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def _count(db: Any, table: str) -> int:
    row = db.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def _status(schema: str) -> dict[str, Any]:
    with connect(schema) as db:
        return dict(
            schema=schema,
            sites=_count(db, "midias_ia_trusted_sources WHERE identity IS NOT NULL"),
            links=_count(db, "bm_links"),
            mechanisms=_count(db, "bm_mechanisms"),
            link_state=_count(db, "bm_link_state"),
            settings=_count(db, "bm_settings"),
        )


def _install(schema: str) -> dict[str, Any]:
    CatalogRepository(schema).install()
    SettingsRepository(schema).install()
    with connect(schema) as db:
        row = db.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_name = ANY(%s)",
            (list(CATALOG_TABLES),),
        ).fetchone()
        assert row is not None
        settings = _count(db, "bm_settings")
    return dict(schema=schema, tables=int(row[0]), settings=settings)


def _unify(schema: str) -> int:
    with connect(schema) as db:
        _print(dict(schema=schema, **unify_sites(db)))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Instala o DDL (idempotente), migra os sites para a tabela do corpus e mostra contagens."""
    parser = argparse.ArgumentParser(prog="buscador_midias db", description=__doc__)
    parser.add_argument("command", choices=("install", "unify-sites", "status"))
    parser.add_argument("--schema", help="Destino (padrão: BM_SCHEMA ou public)")
    args = parser.parse_args(argv)
    try:
        schema = args.schema or configured_schema()
        if args.command == "install":
            _print(_install(schema))
            return 0
        if args.command == "status":
            _print(_status(schema))
            return 0
        return _unify(schema)
    except DatabaseConfigError as error:
        return _fail(str(error))
