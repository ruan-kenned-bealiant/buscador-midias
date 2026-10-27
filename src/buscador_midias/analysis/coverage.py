"""Referência independente de capa e métricas de cobertura, sem acessar provedores."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from lxml import html

from buscador_midias.discovery.url_filter import FilterReason, l0_filter
from buscador_midias.domain.site import Site
from buscador_midias.domain.url import canonicalize_url
from buscador_midias.extraction.html import decode_html
from buscador_midias.strategy.page_classification import classify_url


def editorial_sections(body: bytes, url: str, site: Site, limit: int = 2) -> list[str]:
    """Escolhe editorias reais do menu; nunca inventa caminhos ou muda de edição."""
    labels = (
        "política",
        "politica",
        "politics",
        "economia",
        "economy",
        "business",
        "brasil",
        "brazil",
        "mundo",
        "world",
        "international",
        "tecnologia",
        "technology",
    )
    tree: Any = html.document_fromstring(decode_html(body))
    found: dict[str, int] = {}
    for anchor in tree.xpath("//nav//a[@href]|//header//a[@href]"):
        title = " ".join(anchor.text_content().lower().split())
        if title not in labels:
            continue
        address = urljoin(url, anchor.get("href", ""))
        try:
            if not site.owns(address) or urlsplit(address).query:
                continue
            key = canonicalize_url(address)
            if key != canonicalize_url(url):
                found.setdefault(key, labels.index(title))
        except ValueError:
            continue
    return sorted(found, key=lambda key: found[key])[:limit]


def frontpage_candidates(body: bytes, url: str, site: Site) -> list[dict[str, str]]:
    """Extrai chamadas editoriais visíveis no HTML, sem usar RSS como referência."""
    tree: Any = html.document_fromstring(decode_html(body))
    found: dict[str, dict[str, str]] = {}
    for anchor in tree.xpath("//a[@href]"):
        if anchor.xpath("ancestor::nav|ancestor::header|ancestor::footer"):
            continue
        title = " ".join(
            (anchor.text_content() or anchor.get("aria-label") or anchor.get("title") or "").split()
        )
        cjk = sum("\u3400" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff" for char in title)
        minimum = 8 if cjk >= 4 else 20
        if not minimum <= len(title) <= 500:
            continue
        try:
            address = urljoin(url, anchor.get("href", ""))
            owned = site.owns(address)
        except ValueError:
            continue
        if (
            not owned
            or re.search(
                r"/(?:plus|creativegroup|sponsored|branded|advertising)(?:/|$)",
                urlsplit(address).path,
                re.I,
            )
            or l0_filter(address) in (FilterReason.HOME, FilterReason.ASSET, FilterReason.LISTING)
            or classify_url(address).page_type == "service"
        ):
            continue
        key = canonicalize_url(address)
        if key == canonicalize_url(url):
            continue
        found.setdefault(key, dict(url=address, headline=title))
    return list(found.values())


def eligible(publication: datetime | None, since: datetime, until: datetime) -> str:
    """Data ausente/futura em relação à rodada nunca vira lacuna de cobertura."""
    if publication is None or publication.tzinfo is None:
        return "date_unknown"
    if publication > until:
        return "after_window"
    if publication < since:
        return "before_window"
    return "in_window"


def diagnose(row: dict[str, Any]) -> str:
    """Separa resultado editorial, descoberta, execução e presença real no corpus."""
    if row.get("reference") != "in_window":
        return str(row.get("reference", "inconclusive"))
    if row.get("corpus_id"):
        return "corpus_new" if row.get("corpus_new") else "corpus_already_known"
    if not row.get("discovered"):
        return "discovery_gap"
    state = row.get("job_state")
    status = row.get("article_status")
    if state in ("failed", "cancelled"):
        return "processing_failure"
    if status in ("no_entities", "excluded_content"):
        return "policy_exclusion"
    if status in ("no_text", "date_unknown", "outside_window"):
        return "extraction_or_date_gap"
    if state in ("pending", "queued", "running", "retry"):
        return "processing_pending"
    return "processing_unresolved"


def metrics(rows: list[dict[str, Any]], *, compared: bool = True) -> dict[str, Any]:
    """Não publica porcentagem de cobertura quando não existe referência comparável."""
    comparable = [row for row in rows if row.get("reference") == "in_window"]
    total = len(comparable)
    if not compared:
        return dict(
            reference_articles=total,
            corpus=None,
            discovered=None,
            policy_excluded=None,
            discovery_percent=None,
            corpus_percent=None,
            corpus_percent_without_policy_exclusions=None,
            diagnostics=dict(Counter(str(row.get("reference", "inconclusive")) for row in rows)),
        )
    captured = sum(bool(row.get("corpus_id")) for row in comparable)
    discovered = sum(bool(row.get("discovered")) for row in comparable)
    excluded = sum(diagnose(row) == "policy_exclusion" for row in comparable)
    return dict(
        reference_articles=total,
        corpus=captured,
        discovered=discovered,
        policy_excluded=excluded,
        discovery_percent=round(100 * discovered / total, 2) if total else None,
        corpus_percent=round(100 * captured / total, 2) if total else None,
        corpus_percent_without_policy_exclusions=(
            round(100 * captured / (total - excluded), 2) if total > excluded else None
        ),
        diagnostics=dict(Counter(diagnose(row) for row in rows)),
    )
