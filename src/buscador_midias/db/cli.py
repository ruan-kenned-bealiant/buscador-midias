"""Comando ``python -m buscador_midias db``: instala, importa e confere o cadastro ``bm_*``."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from buscador_midias.db.catalog import CatalogRepository
from buscador_midias.db.connection import DatabaseConfigError, configured_schema, connect
from buscador_midias.db.importer import LEGACY_SCHEMA, ImportMismatchError, import_legacy, verify
from buscador_midias.db.schema import CATALOG_TABLES
from buscador_midias.db.settings import SettingsRepository


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
            sites=_count(db, "bm_sites"),
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


def _import(schema: str, source: str, replace: bool) -> int:
    if schema == source:
        return _fail("Origem e destino são iguais.")
    with connect(schema) as db:
        found = db.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (source,)).fetchone()
        if not found:
            return _fail(f"Schema de origem não encontrado: {source}")
        existing = _count(db, "bm_sites") + _count(db, "bm_links")
        if existing and not replace:
            return _fail("O destino já tem cadastro. Use --replace para recarregá-lo.")
        import_legacy(db, source)
        counts = verify(db, source)
    _print(dict(schema=schema, source=source, imported={k: v[1] for k, v in counts.items()}))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Instala o DDL (idempotente), importa do legado (com conferência) e mostra contagens."""
    parser = argparse.ArgumentParser(prog="buscador_midias db", description=__doc__)
    parser.add_argument("command", choices=("install", "import", "status"))
    parser.add_argument("--schema", help="Destino (padrão: BM_SCHEMA ou public)")
    parser.add_argument("--from", dest="source", default=LEGACY_SCHEMA, help="Schema de origem")
    parser.add_argument(
        "--replace", action="store_true", help="Recarrega o cadastro mesmo se o destino tiver dados"
    )
    args = parser.parse_args(argv)
    try:
        schema = args.schema or configured_schema()
        if args.command == "install":
            _print(_install(schema))
            return 0
        if args.command == "status":
            _print(_status(schema))
            return 0
        return _import(schema, args.source, args.replace)
    except (DatabaseConfigError, ImportMismatchError) as error:
        return _fail(str(error))
