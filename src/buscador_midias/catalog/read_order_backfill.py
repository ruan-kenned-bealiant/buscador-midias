"""Preenche ``links.read_order`` a partir dos documentos já interpretados, sem tocar na rede.

Só preenche link ainda ``unknown``: o que o teste gravou ou uma pessoa corrigiu nunca é
sobrescrito. A coluna é dado derivado, então a versão do link não muda (uma tela aberta não
passa a dar conflito) e a auditoria registra um único evento com o resumo.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from buscador_midias.catalog.read_order import NEWEST_FIRST, UNKNOWN, detect_read_order
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


def demote_unproven_read_order(
    store: CatalogStore, documents: Iterable[tuple[int, str, dict[str, Any]]]
) -> Counter[str]:
    """Volta a ``unknown`` o ``newest_first`` que o documento atual já não comprova.

    Só mexe em ``newest_first`` (o que autoriza o corte); o que uma pessoa marcou de outro jeito
    fica. Não muda a versão do link, como o preenchimento. Contagens: ``kept`` e ``demoted``.
    """
    done: Counter[str] = Counter()
    with store.connection() as db:
        for source_id, url, document in documents:
            row = db.execute(
                "SELECT id FROM links WHERE source_id=? AND url=? AND deleted_at IS NULL "
                "AND read_order=?",
                (source_id, url, NEWEST_FIRST),
            ).fetchone()
            if row is None:
                continue
            if detect_read_order(document) == NEWEST_FIRST:
                done["kept"] += 1
                continue
            db.execute("UPDATE links SET read_order=? WHERE id=?", (UNKNOWN, row["id"]))
            done["demoted"] += 1
        if done["demoted"]:
            store._event(db, "catalog", None, "demote_read_order", None, dict(done))
    return done
