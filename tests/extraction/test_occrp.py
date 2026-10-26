"""Regressão do template público OCCRP: artigo e cards relacionados têm papéis distintos."""

from datetime import UTC, datetime

import pytest

from buscador_midias.extraction.html import extract_article
from buscador_midias.extraction.reliability import PageSnapshot, assess_evidence

NOW = datetime(2026, 10, 26, tzinfo=UTC)
BODY = "Investigators documented transactions and interviewed witnesses in several countries. " * 15


def template(host="www.occrp.org", date="October 22, 2026", heading=True):
    """Página contém cards relacionados maiores que o corpo verdadeiro."""
    title = '<h1 class="article-template__title">Public investigation</h1>' if heading else ""
    return f"""<html><head><meta property="og:url" content="https://{host}/en/news/report">
    <meta property="og:type" content="website"><title>Public investigation</title></head>
    <body>{title}<div class="article-template__content-generator center">
    <div class="article-details__date">{date}</div>
    <div class="content-generator"><div class="wp-wysiwyg"><p>{BODY}</p></div></div></div>
    <article><p>{"Related article only " * 100}</p></article></body></html>"""


def test_occrp_body_date_and_evidence():
    """Identifica artigo com og:type website e extrai seu texto, não o card relacionado."""
    source = template()
    result = extract_article(source, now=NOW)
    assert result.is_article
    assert result.text == BODY.strip()
    assert result.published_at == datetime(2026, 10, 22, tzinfo=UTC)
    page = PageSnapshot(
        "https://www.occrp.org/en/news/report", 200, "text/html", None, source.encode()
    )
    assert assess_evidence(page, expected_headline="Public investigation", now=NOW).ok


@pytest.mark.parametrize(
    "changes",
    [
        dict(host="other.example"),
        dict(date="bad"),
        dict(date="October 22, 2099"),
        dict(heading=False),
    ],
)
def test_not_generic_article_or_related_cards(changes):
    """Template fora do host ou sem provas próprias continua sem artigo confirmado."""
    assert not extract_article(template(**changes), now=NOW).is_article
