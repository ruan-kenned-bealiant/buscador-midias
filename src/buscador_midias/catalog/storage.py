"""Escolha explícita do repositório; após corte, falha PostgreSQL nunca volta ao SQLite."""

import json
import os
from pathlib import Path

from buscador_midias.catalog.postgres import DEFAULT_SCHEMA, PostgresCatalogStore
from buscador_midias.catalog.store import CatalogStore


def open_catalog(
    path: Path, snapshot: Path, sites: Path, *, explicit_sqlite: bool = False
) -> CatalogStore:
    """Abre a base escolhida sem sincronização ou escrita dupla."""
    config = path.parent / "storage.json"
    settings = json.loads(config.read_text()) if config.is_file() and not explicit_sqlite else {}
    backend = (
        "sqlite"
        if explicit_sqlite
        else os.environ.get("BM_CATALOG_BACKEND", settings.get("backend", "sqlite"))
    )
    if backend == "postgres":
        return PostgresCatalogStore(
            os.environ.get("BM_CATALOG_SCHEMA", settings.get("schema", DEFAULT_SCHEMA))
        )
    if backend != "sqlite":
        raise ValueError("Backend do catálogo inválido.")
    return CatalogStore(path, snapshot, sites)
