"""Exclui (exclusão lógica) os links de um site cujo conteúdo já está nos outros links dele.

Usa os documentos em cache da última leitura: só consideram-se links ativos com documento, e o
que cada um lista. Sem ``--apply`` só mostra o que faria. A exclusão passa pelo cadastro, na
revisão lida, e fica na auditoria. É feita por exclusão (não inativação) para a rotina de
saúde não reativar o link.
"""

from __future__ import annotations

import argparse
import gzip
import json

from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.catalog.redundancy import redundant_links
from buscador_midias.catalog.store import CatalogError


def main() -> int:
    """Calcula os redundantes de um site e, com ``--apply``, os exclui."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    store = PostgresCatalogStore()
    with store.connection() as db:
        links = {
            r["url"]: dict(r)
            for r in db.execute(
                "SELECT l.id,l.version,l.url FROM links l JOIN sites s ON s.id=l.source_id "
                "WHERE l.source_id=%s AND l.active=1 AND l.deleted_at IS NULL "
                "AND s.deleted_at IS NULL",
                (args.site_id,),
            ).fetchall()
        }
        documents = {
            r["url"]: r["document"]
            for r in db.execute(
                "SELECT url,document FROM ingest_document_cache WHERE source_id=%s", (args.site_id,)
            ).fetchall()
        }
    articles = {
        url: set(json.loads(gzip.decompress(documents[url])).get("entries") or [])
        for url in links
        if url in documents
    }
    extra = redundant_links(articles)
    union = set().union(*articles.values()) if articles else set()
    print(
        f"links ativos: {len(links)} | com documento: {len(articles)} | artigos distintos: "
        f"{len(union)} | redundantes: {len(extra)} | ficam: {len(links) - len(extra)}"
    )
    if not args.apply:
        return 0
    done = conflicts = 0
    for url in extra:
        link = links[url]
        try:
            store.delete("endpoint", link["id"], link["version"])
            done += 1
        except CatalogError:
            conflicts += 1
    print(f"excluídos: {done} | conflitos (editados no meio): {conflicts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
