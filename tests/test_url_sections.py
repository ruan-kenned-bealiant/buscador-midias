"""Seções de um site que moram em outro domínio: a URL já diz que a leitura não vai render."""

from datetime import UTC, datetime

from buscador_midias.ingestion.flow import excluded_section, url_outside

SINCE = datetime(2026, 10, 29, 3, tzinfo=UTC)


def test_scmp_sections_hosted_elsewhere_are_excluded():
    for path in ("yp/article/1", "spark/being-well", "posties", "young-post/x/y", "goldthread"):
        found = excluded_section(f"https://www.scmp.com/{path}")
        assert found == dict(rule="excluded_section", host="scmp.com", segment=path.split("/")[0])


def test_the_main_scmp_news_stay():
    assert excluded_section("https://www.scmp.com/news/china/politics/article/3331/x") is None
    assert excluded_section("https://www.scmp.com/business/article/3332/y") is None


def test_the_rule_is_scoped_to_the_host():
    assert excluded_section("https://www.example.com/yp/article/1") is None
    assert excluded_section("https://yp.scmp.com/article/1") is None


def test_the_segment_is_whole_and_case_insensitive():
    assert excluded_section("https://www.scmp.com/ypsilon/story") is None
    assert excluded_section("https://SCMP.com/YP/story") is not None
    assert excluded_section("not a url") is None


def test_the_url_filter_applies_the_rule():
    found = url_outside("https://www.scmp.com/yp/article/1", SINCE)
    assert found and found["rule"] == "excluded_section"
