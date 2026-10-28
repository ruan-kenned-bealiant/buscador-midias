# ruff: noqa: F811 -- fixtures de test_link_review são reutilizadas por nome.
"""Preenche a ordem de leitura dos links a partir dos documentos já em cache, sem rede."""

from __future__ import annotations

import pytest
from test_link_review import HOSTS, add_link, store  # noqa: F401
from test_read_order import document

from buscador_midias.catalog.read_order_backfill import backfill_read_order


def order(store, link_id):
    with store.connection() as db:
        return db.execute("SELECT read_order,version FROM links WHERE id=?", (link_id,)).fetchone()


def doc(source_id, path, hours):
    return (source_id, f"https://{HOSTS[source_id]}/{path}", document(hours))


@pytest.fixture
def links(store):
    return dict(
        novo=add_link(store, 1, "novo"),
        antigo=add_link(store, 1, "antigo"),
        bagunca=add_link(store, 2, "bagunca"),
        manual=add_link(store, 2, "manual"),
    )


def docs():
    return [
        doc(1, "novo", list(range(40, 0, -1))),
        doc(1, "antigo", list(range(1, 41))),
        doc(2, "bagunca", [5, 30, 2, 40, 7, 22, 1, 35, 9, 18]),
        doc(2, "manual", list(range(40, 0, -1))),
        doc(2, "sem-link", list(range(40, 0, -1))),
    ]


def test_each_link_gets_the_order_of_its_own_document(store, links):
    done = backfill_read_order(store, docs())
    assert order(store, links["novo"]["id"])[0] == "newest_first"
    assert order(store, links["antigo"]["id"])[0] == "oldest_first"
    assert order(store, links["bagunca"]["id"])[0] == "unordered"
    assert done == {"newest_first": 2, "oldest_first": 1, "unordered": 1, "no_link": 1}


def test_a_value_set_by_hand_is_never_overwritten(store, links):
    manual = links["manual"]
    store.save("endpoint", dict(version=manual["version"], read_order="unordered"), manual["id"])
    backfill_read_order(store, docs())
    assert order(store, manual["id"])[0] == "unordered"


def test_it_does_not_bump_the_version_so_open_edits_do_not_conflict(store, links):
    before = order(store, links["novo"]["id"])[1]
    backfill_read_order(store, docs())
    assert order(store, links["novo"]["id"])[1] == before


def test_a_document_that_says_nothing_leaves_the_link_unknown(store, links):
    done = backfill_read_order(store, [doc(1, "novo", [5] * 12)])
    assert order(store, links["novo"]["id"])[0] == "unknown"
    assert done == {"unknown": 1}


def test_dry_run_changes_nothing_but_still_counts(store, links):
    done = backfill_read_order(store, docs(), apply=False)
    assert order(store, links["novo"]["id"])[0] == "unknown"
    assert done["newest_first"] == 2


def test_one_audit_event_summarises_the_whole_backfill(store, links):
    backfill_read_order(store, docs())
    with store.connection() as db:
        rows = db.execute(
            "SELECT action FROM events WHERE entity='catalog' AND action='backfill_read_order'"
        ).fetchall()
    assert len(rows) == 1
