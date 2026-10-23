"""Valida todos os links atuais, com retomada e no máximo três tentativas por associação."""

from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
from pathlib import Path

from buscador_midias.catalog.batch import ValidationBatch, create_run, status
from buscador_midias.catalog.store import CatalogStore, _stamp


def main() -> None:
    """Executa uma única rodada finita; não cria coleta contínua ou agendamento."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", help="ID de uma rodada existente")
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()
    if not 1 <= args.workers <= 64:
        parser.error("Use de 1 a 64 trabalhadores.")
    root = Path("var/gerenciador")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "validacao.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = CatalogStore(
            root / "catalogo.sqlite",
            Path("var/consulta-funcionais/retrato.json"),
            Path("data/sites.csv"),
        )
        if not args.resume:
            backup = root / (
                "antes-validacao-" + _stamp().replace(":", "").replace("+", "") + ".sqlite"
            )
            with store.connection() as src, sqlite3.connect(backup) as dst:
                src.backup(dst)
        ident = args.resume or create_run(store)
        folder = root / "validacoes" / ident
        folder.mkdir(parents=True, exist_ok=True)
        batch = ValidationBatch(store, ident, workers=args.workers)
        print(json.dumps(status(store, ident)), flush=True)
        try:
            batch.collect()
            batch.finalize()
        except BaseException as error:
            with store.connection() as db:
                db.execute(
                    "UPDATE catalog_validation_runs SET status='paused',detail=?,updated_at=? "
                    "WHERE id=?",
                    (str(error)[:500], _stamp(), ident),
                )
            batch.report(folder)
            raise
        batch.report(folder)
        print(json.dumps(status(store, ident)), flush=True)


if __name__ == "__main__":
    main()
