"""Tema inútil de um link, pelo caminho da URL e pelo rótulo cadastrado."""

from __future__ import annotations

import pytest

from buscador_midias.catalog.link_theme import link_theme


@pytest.mark.parametrize(
    ("url", "label", "theme"),
    [
        ("https://a.example/feeds/horoscopo.xml", "", "horoscope"),
        ("https://a.example/rss/weather", "", "weather"),
        ("https://a.example/previsao-do-tempo/feed", "", "weather"),
        ("https://a.example/clima/rss", "", "weather"),
        ("https://a.example/receitas/feed.xml", "", "cooking"),
        ("https://a.example/gastronomia/rss", "", "cooking"),
        ("https://a.example/rss/loteria", "", "lottery"),
        ("https://a.example/entretenimento/feed", "", "entertainment"),
        ("https://a.example/rss/cinema.xml", "", "entertainment"),
        ("https://a.example/celebrities/rss", "", "entertainment"),
        ("https://a.example/famosos/feed", "", "entertainment"),
        ("https://a.example/estilo-de-vida/rss", "", "lifestyle"),
        ("https://a.example/fashion/feed", "", "lifestyle"),
        ("https://a.example/viagem/rss", "", "lifestyle"),
        ("https://a.example/feed", "Horóscopo do dia", "horoscope"),
        ("https://a.example/feed", "Cinema e TV", "entertainment"),
        ("https://a.example/feed", "Previsão do tempo", "weather"),
    ],
)
def test_useless_themes_are_recognised_from_the_path_or_the_label(url, label, theme):
    assert link_theme(url, label)["theme"] == theme
    assert link_theme(url, label)["action"] == "remove"


@pytest.mark.parametrize(
    "url",
    [
        "https://a.example/esporte/rss",
        "https://a.example/sports/feed.xml",
        "https://a.example/futebol/feed",
    ],
)
def test_sports_links_are_flagged_for_review_never_removed(url):
    found = link_theme(url, "")
    assert found["theme"] == "sports"
    assert found["action"] == "review"


@pytest.mark.parametrize(
    ("url", "label"),
    [
        ("https://a.example/feed", ""),
        ("https://a.example/rss/politica", "Política"),
        ("https://a.example/world/climate-change/rss", "Climate"),
        ("https://a.example/investigacoes/feed", "Investigações"),
        ("https://a.example/economia/rss", "Economia"),
        ("https://a.example/policia/feed", "Polícia"),
        ("https://a.example/rss/cinematica", ""),
        ("https://a.example/rss/familia", ""),
    ],
)
def test_news_links_have_no_useless_theme(url, label):
    assert link_theme(url, label) is None


def test_the_host_never_counts_only_the_path():
    assert link_theme("https://receitas.example/feed", "") is None
    assert link_theme("https://cinema.example/politica/feed", "") is None
