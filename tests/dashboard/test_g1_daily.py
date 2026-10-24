"""Seleção explícita do g1 não depende de varrer o arquivo histórico."""

from datetime import UTC, datetime

import pytest

from buscador_midias.catalog.mechanisms import get_mechanism

ROOT = "https://g1.globo.com/sitemap/g1/"


def document(children):
    body = (
        "<sitemapindex>"
        + "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in children)
        + "</sitemapindex>"
    ).encode()
    model = get_mechanism("g1_daily")
    return model, model.read(body, ROOT + "sitemap.xml", model.defaults())


def test_selects_all_pages_of_window_and_flags_unknown_structure():
    old = [ROOT + f"2001/01/01_{page}.xml" for page in range(9000)]
    recent = [ROOT + f"2026/10/20_{page}.xml" for page in range(1, 4)]
    unknown = [ROOT + "latest.xml", ROOT + "2026/02/30_1.xml"]
    model, parsed = document(old + recent + unknown + recent)
    config = dict(model.defaults(), updates_overlap_days=0)
    plan = model.plan_collection(
        parsed,
        datetime(2026, 10, 20, tzinfo=UTC),
        datetime(2026, 10, 20, 23, tzinfo=UTC),
        config,
    )
    assert set(plan.children) == set(recent)
    assert plan.skipped == 9000
    assert plan.unrecognized == tuple(unknown)
    # Reusing the full parsed index with another window must not reuse a filtered subset.
    earlier = model.plan_collection(
        parsed,
        datetime(2001, 1, 1, tzinfo=UTC),
        datetime(2001, 1, 1, 23, tzinfo=UTC),
        config,
    )
    assert len(earlier.children) == 9000


def test_timezone_and_overlap_include_neighboring_partitions():
    urls = [ROOT + f"2026/10/{day}_1.xml" for day in range(17, 24)]
    model, parsed = document(urls)
    since = datetime(2026, 10, 20, 1, tzinfo=UTC)
    until = datetime(2026, 10, 20, 2, tzinfo=UTC)
    config = dict(model.defaults(), updates_timezone="America/Sao_Paulo")
    plan = model.plan_collection(parsed, since, until, config)
    assert set(plan.children) == {ROOT + f"2026/10/{day}_1.xml" for day in (18, 19, 20)}


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/sitemap.xml",
        "https://g1.globo.com/sitemap/videos/sitemap.xml",
        "https://g1.globo.com.evil.example/sitemap/g1/sitemap.xml",
    ],
)
def test_rejects_other_portals_and_video_index(url):
    model = get_mechanism("g1_daily")
    with pytest.raises(ValueError):
        model.read(b"<urlset/>", url, model.defaults())


def test_generic_index_preserves_all_declared_children():
    urls = [ROOT + "2001/01/01_1.xml", ROOT + "latest.xml"]
    _, parsed = document(urls)
    model = get_mechanism("sitemap_tree")
    plan = model.plan_collection(
        parsed,
        datetime(2026, 10, 20, tzinfo=UTC),
        datetime(2026, 10, 21, tzinfo=UTC),
        model.defaults(),
    )
    assert plan.children == tuple(urls)
