"""Preenche ``links.read_order`` a partir dos documentos já interpretados, sem tocar na rede.

Só preenche link ainda ``unknown``: o que o teste gravou ou uma pessoa corrigiu nunca é
sobrescrito. A coluna é dado derivado, então a versão do link não muda (uma tela aberta não
passa a dar conflito) e a auditoria registra um único evento com o resumo.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from buscador_midias.catalog.read_order import UNKNOWN, detect_read_order
from buscador_midias.catalog.store import CatalogStore


def backfill_read_order(
    store: CatalogStore,
    documents: Iterable[tuple[int, str, dict[str, Any]]],
    *,
    apply: bool = True,
) -> Counter[str]:
    """Classifica cada ``(site, url, documento)`` e grava no link correspondente.

    Contagens: a ordem encontrada, ``no_link`` (documento sem link ativo ou inativo no
    cadastro) e ``already_set`` (link que já tinha ordem). Com ``apply=False`` só conta.
    """
    done: Counter[str] = Counter()
    with store.connection() as db:
        for source_id, url, document in documents:
            row = db.execute(
                "SELECT id,read_order FROM links "
                "WHERE source_id=? AND url=? AND deleted_at IS NULL",
                (source_id, url),
            ).fetchone()
            if row is None:
                done["no_link"] += 1
                continue
            if row["read_order"] != UNKNOWN:
                done["already_set"] += 1
                continue
            found = detect_read_order(document)
            done[found] += 1
            if apply and found != UNKNOWN:
                db.execute("UPDATE links SET read_order=? WHERE id=?", (found, row["id"]))
        if apply:
            store._event(db, "catalog", None, "backfill_read_order", None, dict(done))
    return done
