from buscador_midias.catalog.editorial_mapping import normalized, purpose, recent_samples


def test_recent_proof_requires_article_text_and_publication_within_window():
    samples = [
        dict(url="recent", text=True, article=True, published_at="2026-10-26T10:00:00+00:00"),
        dict(url="old", text=True, article=True, published_at="2024-10-26T10:00:00+00:00"),
        dict(url="undated", text=True, article=True, published_at=""),
        dict(url="future", text=True, article=True, published_at="2026-10-27T10:00:00+00:00"),
        dict(url="shell", text=False, article=True, published_at="2026-10-26T10:00:00+00:00"),
        dict(url="landing", text=True, article=False, published_at="2026-10-26T10:00:00+00:00"),
    ]
    result = dict(window_until="2026-10-26T14:00:00+00:00", samples=samples)
    assert [s["url"] for s in recent_samples(result)] == ["recent"]


def test_fixed_archive_leaf_does_not_become_daily_route_even_with_recent_news():
    assert purpose(dict(url="https://g1.globo.com/sitemap/g1/2026/10/26.xml")) == "archive"
    assert purpose(dict(url="https://news.example/sitemap.xml?year=2026")) == "archive"
    assert purpose(dict(url="https://news.example/news.xml")) == "updates"


def test_huge_generic_index_requires_review_but_g1_has_an_explicit_daily_plan():
    result = dict(
        url="https://news.example/index.xml",
        root_index=True,
        root_children=6000,
        model="sitemap_tree",
    )
    assert purpose(result) == "review"
    assert purpose(dict(result, model="g1_daily")) == "updates"


def test_cosmetic_duplicates_preserve_meaningful_queries():
    assert normalized("https://www.news.example/feed/") == normalized("http://news.example/feed")
    assert normalized("https://news.example/feed?section=world") != normalized(
        "https://news.example/feed?section=business"
    )
