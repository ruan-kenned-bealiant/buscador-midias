"""Catálogo de temas do KyX: cobre os slugs da análise e protege os ids do front."""

from __future__ import annotations

import pytest

from buscador_midias.analysis.themes import THEME_SLUGS
from buscador_midias.corpus.theme_catalog import theme_refs

UNMAPPED = {"processos_judiciais"}
MAPPED = [slug for slug in THEME_SLUGS if slug not in UNMAPPED]


@pytest.mark.parametrize("slug", MAPPED)
def test_every_mapped_slug_has_a_theme_for_both_entity_kinds(slug):
    assert theme_refs(slug, "PF") and theme_refs(slug, "PJ")


@pytest.mark.parametrize("slug", sorted(UNMAPPED))
def test_unmapped_slugs_have_no_theme(slug):
    assert theme_refs(slug, "PF") == () and theme_refs(slug, "PJ") == ()


def test_unknown_slug_has_no_theme():
    assert theme_refs("futebol", "PF") == ()


@pytest.mark.parametrize("slug", MAPPED)
def test_person_and_company_never_share_a_theme_id(slug):
    people = {r.theme_id for r in theme_refs(slug, "PF")}
    companies = {r.theme_id for r in theme_refs(slug, "PJ")}
    assert not people & companies


def test_each_theme_has_a_national_and_a_foreign_variant_except_ethics():
    for slug in MAPPED:
        expected = 1 if slug == "etica_integridade" else 2
        assert len(theme_refs(slug, "PF")) == len(theme_refs(slug, "PJ")) == expected


def test_other_entity_kinds_follow_the_company_themes():
    assert theme_refs("corrupcao", "OUTRO") == theme_refs("corrupcao", "PJ")


def test_ids_match_the_kyx_catalog():
    assert [r.theme_id for r in theme_refs("corrupcao", "PF")] == [1, 15]
    assert [r.theme_id for r in theme_refs("corrupcao", "PJ")] == [2, 16]
    assert [r.theme_id for r in theme_refs("ciber_seguranca", "PJ")] == [43, 55]


def test_every_theme_carries_name_and_description():
    for slug in MAPPED:
        for ref in (*theme_refs(slug, "PF"), *theme_refs(slug, "PJ")):
            assert ref.name.strip() and ref.description.strip()
