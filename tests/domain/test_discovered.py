from datetime import UTC, datetime

from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.domain.sitemap import SitemapEntry
from buscador_midias.domain.url import url_hash


def test_from_entry_calcula_o_hash_canonico_e_guarda_a_origem():
    entry = SitemapEntry(
        loc="http://WWW.Exemplo.com/politica/materia/?utm_source=x",
        lastmod=datetime(2026, 10, 15, tzinfo=UTC),
        title="Título",
        publication_date=datetime(2026, 10, 14, 8, tzinfo=UTC),
    )

    found = DiscoveredUrl.from_entry(
        entry, site_key="exemplo.com", source_sitemap="https://x/s.xml"
    )

    assert found.url == entry.loc
    assert found.url_hash == url_hash("https://exemplo.com/politica/materia")
    assert found.site_key == "exemplo.com"
    assert found.source_sitemap == "https://x/s.xml"
    assert found.title == "Título"


def test_timestamp_prefere_a_publicacao_e_cai_no_lastmod():
    lastmod = datetime(2026, 10, 15, tzinfo=UTC)
    published = datetime(2026, 10, 14, tzinfo=UTC)

    with_both = DiscoveredUrl.from_entry(
        SitemapEntry(loc="https://a.com/x", lastmod=lastmod, publication_date=published),
        site_key="a.com",
        source_sitemap="s",
    )
    only_lastmod = DiscoveredUrl.from_entry(
        SitemapEntry(loc="https://a.com/y", lastmod=lastmod), site_key="a.com", source_sitemap="s"
    )
    neither = DiscoveredUrl.from_entry(
        SitemapEntry(loc="https://a.com/z"), site_key="a.com", source_sitemap="s"
    )

    assert with_both.timestamp == published
    assert only_lastmod.timestamp == lastmod
    assert neither.timestamp is None
