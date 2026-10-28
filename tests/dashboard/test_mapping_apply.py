# ruff: noqa: F811 -- fixtures de test_link_review são reutilizadas por nome.
"""Aplicação das propostas de mapeamento: só entra link provado, vivo e com tema útil."""

from __future__ import annotations

import json

import pytest
from test_link_review import HOSTS, NOW, Fleet, add_link, store  # noqa: F401

from buscador_midias.catalog.mapping_apply import apply_mapping, vet_candidates


def proof_of(store, source_id, url, model="rss", kind="feed", transport="direct"):
    return dict(
        test_id=f"t-{url}",
        model_version=store.model_settings(model)["version"],
        source_id=source_id,
        source_url=store.source(source_id)["url"],
        url=url,
        model=model,
        requested_kind=kind,
        access_transport=transport,
        xml_valid=True,
        compatible=True,
        root_entries=3,
        root_children=0,
        root_index=False,
        observed_at=NOW.isoformat(),
        text_samples=[],
        samples=[],
        outcome="text_confirmed",
    )


def candidate(store, source_id, path, **changes):
    url = f"https://{HOSTS[source_id]}/{path}"
    base = dict(source_id=source_id, url=url, kind="feed", model="rss", label="")
    base.update(changes)
    return dict(**base, result=proof_of(store, source_id, url, base["model"], base["kind"]))


def test_only_candidates_with_news_this_month_are_kept(store):
    found = vet_candidates(
        [candidate(store, 1, "novo"), candidate(store, 1, "parado"), candidate(store, 2, "morto")],
        NOW,
        Fleet({"novo": 2, "parado": 90, "morto": 500}),
    )
    assert {i["url"].rsplit("/", 1)[1]: i["proposal"] for i in found} == {
        "novo": "keep",
        "parado": "inactivate",
        "morto": "delete",
    }


def test_a_useless_theme_is_rejected_without_reading_it(store):
    fleet = Fleet({"receitas": 1})
    (found,) = vet_candidates([candidate(store, 1, "receitas")], NOW, fleet)
    assert found["proposal"] == "remove_theme"
    assert fleet.asked == []


def test_vetting_keeps_the_proof_that_came_with_the_candidate(store):
    (found,) = vet_candidates([candidate(store, 1, "novo")], NOW, Fleet({"novo": 1}))
    assert found["result"]["test_id"] == "t-https://alfa.example/novo"


def test_a_kept_candidate_becomes_an_active_link_with_its_proof(store):
    vetted = vet_candidates([candidate(store, 1, "novo")], NOW, Fleet({"novo": 1}))
    done = apply_mapping(store, vetted)
    assert done == {"created": 1}
    (row,) = [r for r in all_links(store) if r["url"].endswith("novo")]
    assert row["active"]
    assert json.loads(row["last_test"])["test_id"] == "t-https://alfa.example/novo"


def test_nothing_but_keep_is_created(store):
    vetted = vet_candidates(
        [candidate(store, 1, "parado"), candidate(store, 1, "receitas")],
        NOW,
        Fleet({"parado": 90, "receitas": 1}),
    )
    assert apply_mapping(store, vetted) == {"skipped": 2}
    assert not any(r["url"].endswith(("parado", "receitas")) for r in all_links(store))


def all_links(store):
    with store.connection() as db:
        return [dict(r) for r in db.execute("SELECT * FROM links").fetchall()]


def test_a_link_already_registered_is_not_recreated(store):
    add_link(store, 1, "novo", active=False)
    vetted = vet_candidates([candidate(store, 1, "novo")], NOW, Fleet({"novo": 1}))
    assert apply_mapping(store, vetted) == {"exists": 1}
    (row,) = [r for r in all_links(store) if r["url"].endswith("novo")]
    assert not row["active"]


def test_a_deleted_link_is_not_resurrected(store):
    link = add_link(store, 1, "novo")
    store.delete("endpoint", link["id"], link["version"])
    vetted = vet_candidates([candidate(store, 1, "novo")], NOW, Fleet({"novo": 1}))
    assert apply_mapping(store, vetted) == {"exists": 1}


def test_a_proof_from_another_configuration_is_refused_not_applied(store):
    bad = candidate(store, 1, "novo")
    bad["result"]["model"] = "sitemap"
    vetted = vet_candidates([bad], NOW, Fleet({"novo": 1}))
    assert apply_mapping(store, vetted) == {"refused": 1}
    assert not any(r["url"].endswith("novo") for r in all_links(store))


@pytest.mark.parametrize("age", [0, 29])
def test_the_edge_of_the_month_still_counts_as_alive(store, age):
    (found,) = vet_candidates([candidate(store, 1, "novo")], NOW, Fleet({"novo": age}))
    assert found["proposal"] == "keep"
