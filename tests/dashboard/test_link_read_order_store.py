# ruff: noqa: F811 -- fixtures de test_link_review são reutilizadas por nome.
"""A ordem de leitura vive no link: vem do teste, pode ser corrigida e some se o link muda."""

from __future__ import annotations

import pytest
from test_link_review import add_link, store  # noqa: F401
from test_mapping_apply import proof_of

from buscador_midias.catalog.store import CatalogError


def read_order(store, link_id):
    with store.connection() as db:
        return db.execute("SELECT read_order FROM links WHERE id=?", (link_id,)).fetchone()[0]


def test_a_new_link_starts_unknown(store):
    assert read_order(store, add_link(store, 1, "feed.xml")["id"]) == "unknown"


def test_it_can_be_set_by_hand_with_a_valid_value(store):
    link = add_link(store, 1, "feed.xml")
    store.save("endpoint", dict(version=link["version"], read_order="newest_first"), link["id"])
    assert read_order(store, link["id"]) == "newest_first"


def test_an_invalid_value_is_refused(store):
    link = add_link(store, 1, "feed.xml")
    with pytest.raises(CatalogError, match="Ordem"):
        store.save("endpoint", dict(version=link["version"], read_order="aleatoria"), link["id"])
    assert read_order(store, link["id"]) == "unknown"


def test_the_test_that_proves_a_link_also_records_its_order(store):
    url = "https://alfa.example/feed.xml"
    proof = proof_of(store, 1, url) | dict(read_order="newest_first")
    row = store.save(
        "endpoint",
        dict(url=url, kind="feed", model="rss", active=True),
        source_id=1,
        test_result=proof,
    )
    assert read_order(store, row["id"]) == "newest_first"


def test_a_proof_without_an_order_leaves_it_unknown(store):
    url = "https://alfa.example/feed.xml"
    row = store.save(
        "endpoint",
        dict(url=url, kind="feed", model="rss", active=True),
        source_id=1,
        test_result=proof_of(store, 1, url),
    )
    assert read_order(store, row["id"]) == "unknown"


@pytest.mark.parametrize(
    "change", [dict(url="https://alfa.example/outro.xml"), dict(access_transport="curl_chrome_h2")]
)
def test_changing_what_the_link_reads_resets_the_order(store, change):
    link = add_link(store, 1, "feed.xml")
    link = store.save(
        "endpoint", dict(version=link["version"], read_order="newest_first"), link["id"]
    )
    store.save("endpoint", dict(version=link["version"], **change), link["id"])
    assert read_order(store, link["id"]) == "unknown"


def test_editing_only_the_label_keeps_the_order(store):
    link = add_link(store, 1, "feed.xml")
    link = store.save(
        "endpoint", dict(version=link["version"], read_order="newest_first"), link["id"]
    )
    store.save("endpoint", dict(version=link["version"], label="Capa"), link["id"])
    assert read_order(store, link["id"]) == "newest_first"
