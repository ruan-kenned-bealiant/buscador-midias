import pytest

from buscador_midias.corpus.country import registered_country, resolve_country


@pytest.mark.parametrize(
    ("language", "article", "site", "expected"),
    [
        ("pt-BR", "PT", "GB", ("BR", "language")),
        ("pt-PT", "BR", "GB", ("PT", "language")),
        ("pt", "PT", "BR", ("PT", "article")),
        ("en", None, "GB", ("GB", "site")),
        ("en-US", None, "GB", ("GB", "site")),
        ("en-US", "US", "GB", ("US", "article")),
        ("es-419", None, "AR", ("AR", "site")),
        ("zh-Hant-TW", "CN", None, ("TW", "language")),
        ("pt_UN", "ZZ", "INT", (None, None)),
        (None, "China", " BR / GB / BR ", ("BR/GB", "site")),
        (None, "UN", "UN/EU/AN/PO", (None, None)),
        (None, None, None, (None, None)),
    ],
)
def test_country_precedence_and_unknowns(language, article, site, expected):
    assert resolve_country(language, article, site) == expected


def test_registry_prefers_specific_host_and_path_without_matching_newsletter():
    registry = {
        "example.org": "BR",
        "example.org/news": "GB",
        "world.example.org": "FR",
        "example.org/noticias": "PT",
    }
    assert registered_country("https://example.org/news/a", registry) == "GB"
    assert registered_country("https://example.org/newsletter/a", registry) == "BR"
    assert registered_country("https://world.example.org/news/a", registry) == "FR"
    assert registered_country("https://www.example.org/noticias/a", registry) == "PT"
    assert registered_country("https://example.org.evil.test/a", registry) is None


def test_registry_without_covering_path_requires_agreement():
    registry = {"example.org/br": "BR", "example.org/pt": "PT"}
    assert registered_country("https://example.org/other", registry) is None
    assert registered_country("https://example.org/pt/a", registry) == "PT"
