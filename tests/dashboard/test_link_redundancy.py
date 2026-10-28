"""Links de um mesmo site cujo conteúdo já está inteiro em outros são redundantes."""

from __future__ import annotations

from buscador_midias.catalog.redundancy import redundant_links


def test_a_link_whose_articles_are_all_elsewhere_is_redundant():
    links = {"main": {"a", "b", "c"}, "tag": {"a", "b"}}
    assert redundant_links(links) == ["tag"]


def test_a_link_with_even_one_article_nobody_else_has_is_kept():
    links = {"main": {"a", "b", "c"}, "tag": {"a", "z"}}
    assert redundant_links(links) == []


def test_many_subsets_collapse_into_the_few_links_that_cover_everything():
    links = {"main": set("abcdef"), **{f"tag{i}": {c} for i, c in enumerate("abcdef")}}
    assert sorted(redundant_links(links)) == sorted(f"tag{i}" for i in range(6))


def test_identical_links_keep_exactly_one():
    links = {"x": {"a", "b"}, "y": {"a", "b"}, "z": {"a", "b"}}
    assert len(redundant_links(links)) == 2


def test_the_choice_is_deterministic_on_ties():
    links = {"y": {"a"}, "x": {"a"}}
    assert redundant_links(links) == redundant_links(dict(reversed(links.items())))


def test_a_link_with_no_articles_is_never_called_redundant():
    assert redundant_links({"main": {"a"}, "empty": set()}) == []


def test_overlapping_links_keep_the_smallest_set_that_covers_all():
    links = {"p": {"a", "b"}, "q": {"b", "c"}, "r": {"a", "c"}}
    assert len(redundant_links(links)) == 1
