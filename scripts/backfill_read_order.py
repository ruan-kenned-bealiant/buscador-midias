"""Cria a coluna ``links.read_order`` (se faltar) e a preenche dos documentos já em cache.

Sem ``--apply`` só conta o que seria gravado. Não toca na rede. Idempotente: link que já tem
ordem (do teste ou de edição manual) nunca é sobrescrito.
"""

from __future__ import annotations

import argparse
import gzip
import json

from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.catalog.read_order_backfill import backfill_read_order

COLUMN = (
    "ALTER TABLE links ADD COLUMN IF NOT EXISTS read_order TEXT NOT NULL DEFAULT 'unknown' "
    "CHECK(read_order IN ('newest_first','oldest_first','unordered','unknown'))"
)
LATEST = (
    "SELECT DISTINCT ON (source_id,url) source_id,url,document FROM ingest_document_cache "
    "ORDER BY source_id,url,checked_at DESC"
)


def main() -> int:
    """Executa a migração da coluna e o preenchimento."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    apply = parser.parse_args().apply
    store = PostgresCatalogStore()
    if apply:
        with store.connection() as db:
            db.execute(COLUMN)
    with store.connection() as db:
        has = db.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='links' "
            "AND column_name='read_order'"
        ).fetchone()
        rows = db.execute(LATEST).fetchall() if has else []
    if not has:
        print("Coluna read_order ainda não existe; rode com --apply.")
        return 0

    def documents():
        for row in rows:
            document = json.loads(gzip.decompress(row["document"]))
            for key in ("entries", "children"):
                document.setdefault(key, [])
            for key in ("dates", "hints", "child_dates"):
                document.setdefault(key, {})
            yield row["source_id"], row["url"], document

    print(dict(backfill_read_order(store, documents(), apply=apply)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
