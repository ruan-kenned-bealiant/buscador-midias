"""Contratos da auditoria independente, sem rede, banco ou fixture PostgreSQL."""

import csv
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from buscador_midias.analysis.coverage import (
    diagnose,
    editorial_sections,
    eligible,
    frontpage_candidates,
    metrics,
)
from buscador_midias.domain.site import Site

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "coverage_audit", ROOT / "scripts/audit_frontpage_coverage.py"
)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def site():
    return Site.create(
        name="Exemplo",
        url="https://news.example",
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )


def test_reference_ignores_navigation_external_and_tracking_duplicates():
    body = b"""<html><nav><a href="/nav">Uma chamada longa na navegacao</a></nav>
    <main><a href="/story?utm_source=a">Uma reportagem realmente na capa</a>
    <a href="/story">Outra chamada para a mesma noticia</a>
    <a href="https://other.example/story">Noticia de outra fonte editorial</a>
    <a href="/tag/economia">Todas as noticias sobre economia</a></main></html>"""
    rows = frontpage_candidates(body, "https://news.example/", site())
    assert len(rows) == 1
    assert rows[0]["url"] == "https://news.example/story?utm_source=a"


def test_short_chinese_headlines_are_not_silently_dropped():
    body = '<html><a href="/story">今日国际经济新闻报道</a></html>'.encode()
    assert len(frontpage_candidates(body, "https://news.example/", site())) == 1


def test_editorial_section_does_not_admit_sibling_editions():
    source = Site.create(
        name="Português",
        url="https://news.example/pt",
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )
    body = (
        b'<a href="/en/story">A long English headline about news</a>'
        b'<a href="/pt/story">Uma chamada da edicao em portugues</a>'
    )
    assert [r["url"] for r in frontpage_candidates(body, source.url, source)] == [
        "https://news.example/pt/story"
    ]


def test_no_reference_is_unknown_instead_of_zero_percent():
    result = metrics([dict(reference="access_inconclusive"), dict(reference="after_window")])
    assert result["reference_articles"] == 0
    assert result["corpus_percent"] is None


def test_uncompared_reference_never_claims_zero_coverage_or_discovery_failure():
    result = metrics([dict(reference="in_window")], compared=False)
    assert result["reference_articles"] == 1
    assert result["corpus"] is None and result["corpus_percent"] is None
    assert "discovery_gap" not in result["diagnostics"]


def test_known_articles_count_as_covered_without_claiming_new_capture():
    rows = [
        dict(reference="in_window", corpus_id=1, corpus_new=False, discovered=True),
        dict(reference="in_window", discovered=True, article_status="no_entities"),
        dict(reference="in_window", discovered=False),
    ]
    result = metrics(rows)
    assert diagnose(rows[0]) == "corpus_already_known"
    assert result["corpus"] == 1 and result["policy_excluded"] == 1
    assert result["corpus_percent"] == 33.33
    assert result["corpus_percent_without_policy_exclusions"] == 50


@pytest.mark.parametrize(
    "offset,expected", [(-1, "before_window"), (0, "in_window"), (1, "after_window")]
)
def test_publication_respects_both_run_boundaries(offset, expected):
    moment = datetime(2026, 10, 26, 18, tzinfo=UTC)
    assert eligible(moment + timedelta(seconds=offset), moment, moment) == expected


def test_date_without_timezone_never_becomes_a_gap():
    now = datetime.now(UTC)
    assert eligible(None, now, now) == "date_unknown"
    assert eligible(now.replace(tzinfo=None), now, now) == "date_unknown"


@pytest.mark.parametrize(
    "row,expected",
    [
        (dict(discovered=False), "discovery_gap"),
        (dict(discovered=True, job_state="failed"), "processing_failure"),
        (dict(discovered=True, article_status="no_text"), "extraction_or_date_gap"),
        (dict(discovered=True, job_state="retry"), "processing_pending"),
        (dict(discovered=True, article_status="excluded_content"), "policy_exclusion"),
    ],
)
def test_diagnostics_do_not_confuse_stages(row, expected):
    assert diagnose(dict(reference="in_window", **row)) == expected


def test_waits_for_exact_run_completion_without_http(monkeypatch, tmp_path):
    responses = [
        dict(id="a" * 32, state="running", finished_at=None),
        dict(id="a" * 32, state="partial", finished_at="2026-10-26T22:00:00Z"),
    ]
    queries = []

    def read(query, parameters):
        queries.append(parameters)
        return [responses.pop(0)]

    pauses = []
    monkeypatch.setattr(audit, "read_db", read)
    monkeypatch.setattr(audit.time, "sleep", pauses.append)
    result = audit.wait_for_run("a" * 32, tmp_path)
    assert result["state"] == "partial" and pauses == [180]
    assert queries == [("a" * 32,), ("a" * 32,)]


def test_target_cohort_is_explicit_unique_and_balanced():
    with (ROOT / "data/coverage_priority_sites.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 120 and len({r["source_id"] for r in rows}) == 120
    assert sum(r["group"] == "Brasil" for r in rows) == 60
    assert {"Reuters", "g1", "CNN International"} <= {r["name"] for r in rows}


def test_canonical_alias_cannot_point_outside_editorial_scope():
    body = b'<link rel="canonical" href="https://external.example/fake">'
    hashes = audit.aliases(dict(url="https://news.example/story"), body, site())
    from buscador_midias.domain.url import url_hash

    assert url_hash("https://external.example/fake") not in hashes
    assert url_hash("https://news.example/story") in hashes


def test_source_records_independent_proof_and_report(monkeypatch, tmp_path):
    import json

    from buscador_midias.net.fetcher import FetchResult

    home = b'<html><main><a href="/story">Uma reportagem factual publicada no dia</a></main></html>'
    schema = json.dumps(
        {
            "@type": "NewsArticle",
            "headline": "Uma reportagem factual publicada no dia",
            "datePublished": "2026-10-26T10:00:00-03:00",
        }
    )
    paragraph = "A reportagem ouviu autoridades e examinou documentos sobre a empresa mencionada. "
    body = (
        "<html><head><title>Uma reportagem factual publicada no dia</title>"
        f'<script type="application/ld+json">{schema}</script></head>'
        "<body><article><h1>Uma reportagem factual publicada no dia</h1>"
        + "".join(f"<p>{paragraph}</p>" for _ in range(30))
        + "</article></body></html>"
    ).encode()

    class Reader:
        requests = 0

        def get(self, url):
            self.requests += 1
            return FetchResult(
                url, 200, home if url == "https://news.example" else body, None, url, "text/html"
            )

        def close(self):
            pass

    def compare(rows, source_id, run_id, cutoff):
        for row in rows:
            row.update(corpus_id=123, corpus_new=True, discovered=True)
            row["diagnostic"] = diagnose(row)

    monkeypatch.setattr(audit, "make_reader", lambda *a, **kw: Reader())
    monkeypatch.setattr(audit, "compare", compare)
    monkeypatch.setattr(audit, "PROOF_BYTES", 0)
    manifest = dict(
        run=dict(id="a" * 32, since="2026-10-26T03:00:00+00:00", until="2026-10-26T20:00:00+00:00"),
        comparison_at="2026-10-26T21:00:00+00:00",
    )
    target = dict(source_id=1, name="Exemplo", homepage="https://news.example", transport="direct")
    result = audit.audit_source(target, manifest, tmp_path)
    assert result["state"] == "sampled", result
    assert result["metrics"]["corpus_percent"] == 100
    assert (tmp_path / result["homepage_proof"]).is_file()
    audit.report([result], manifest, tmp_path)
    assert (tmp_path / "report.html").exists()
    assert "corpus_new" in (tmp_path / "articles.csv").read_text()


def test_failed_homepage_never_counts_as_zero_coverage(monkeypatch, tmp_path):
    from buscador_midias.net.fetcher import FetchResult

    class Reader:
        requests = 0

        def get(self, url):
            return FetchResult(url, 403, b"Forbidden", None)

    monkeypatch.setattr(audit, "make_reader", lambda *a, **kw: Reader())
    result = audit.audit_source(
        dict(source_id=1, name="Exemplo", homepage="https://news.example", transport="direct"),
        {},
        tmp_path,
    )
    assert result["state"] == "inconclusive"
    assert result["reason"] == "homepage_inaccessible_or_challenge"
    assert metrics(result["articles"])["corpus_percent"] is None


def test_sections_use_real_editorial_navigation_with_bounded_same_site_scope():
    body = b"""<nav><a href="/politica">Politica</a><a href="/business">Business</a>
    <a href="/world">World</a><a href="https://other.example/world">World</a>
    <a href="/subscribe">Subscribe</a><a href="/politica?redirect=a">Politica</a></nav>"""
    assert editorial_sections(body, site().url, site()) == [
        "https://news.example/politica",
        "https://news.example/business",
    ]


def test_frozen_reference_is_reused_and_compared_only_after_run_finishes(monkeypatch, tmp_path):
    import json

    target = dict(source_id=1, name="Exemplo", homepage=site().url, transport="direct")
    original = dict(
        target=target,
        articles=[
            dict(
                url=site().url + "/story",
                headline="Uma noticia de referencia",
                reference="in_window",
                hashes=["hash"],
            )
        ],
        state="sampled",
    )
    frozen = tmp_path / "references/1.json"
    audit.write(frozen, original)
    before = frozen.read_bytes()
    audit.write(tmp_path / "catalog-before.json", {})
    run = dict(
        id="a" * 32,
        since="2026-10-26T03:00:00Z",
        until="2026-10-26T22:00:00Z",
        state="running",
        finished_at=None,
    )
    events = []
    monkeypatch.setattr(
        audit, "read_db", lambda query, params: [run] if "SELECT *" in query else []
    )

    def wait(*_):
        events.append("finished")
        return dict(run, state="completed", finished_at="2026-10-27T03:00:00Z")

    def compare(rows, source_id, run_id, cutoff):
        assert events == ["finished"]
        assert cutoff == "2026-10-27T03:00:00Z"
        rows[0].update(corpus_id=12, discovered=True, diagnostic="corpus_new")

    monkeypatch.setattr(audit, "wait_for_run", wait)
    monkeypatch.setattr(audit, "compare", compare)
    monkeypatch.setattr(
        audit, "audit_source", lambda *_: pytest.fail("Amostra deve ser reutilizada")
    )
    assert (
        audit.frozen_audit(dict(run_id=run["id"], targets=[target]), tmp_path, correct_routes=False)
        == 0
    )
    assert frozen.read_bytes() == before
    assert json.loads((tmp_path / "summary.json").read_text())["metrics"]["corpus"] == 1
    assert (tmp_path / "sites.csv").is_file()
    assert "1/1 no corpus" in (tmp_path / "report.html").read_text()
