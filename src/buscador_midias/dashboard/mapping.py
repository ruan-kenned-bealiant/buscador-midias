"""Consulta do mapa estrutural e de amostras observadas, sem chamadas aos sites."""

from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from buscador_midias.domain.mapping import MapPage, PageStatus
from buscador_midias.domain.site import Site
from buscador_midias.sites.loader import LoadResult
from buscador_midias.state.store import StateStore
from buscador_midias.strategy.manual import ManualRulesBook
from buscador_midias.strategy.mechanism_inventory import MechanismIndex, mechanism_record


def safe_site_url(site: Site, value: str | None) -> str | None:
    """Só libera links HTTP do escopo da fonte para a tela."""
    if not value:
        return None
    try:
        return value if urlsplit(value).scheme in ("http", "https") and site.owns(value) else None
    except ValueError:
        return None


def mapping_catalog(
    store: StateStore,
    catalog: LoadResult,
    rules: ManualRulesBook | None = None,
) -> dict[str, Any]:
    """As 1.299 linhas do catálogo, com duplicatas explícitas e contagens por fonte única."""
    overview = store.mapping_overview()
    mechanism_counts: dict[str, dict[str, int]] = {}
    for record in mapping_mechanisms(store, catalog.sites, rules=rules):
        counts = mechanism_counts.setdefault(
            record["site_key"], {"recent": 0, "supplement": 0, "review": 0}
        )
        counts[record["group"]] += 1
    entries: list[dict[str, Any]] = []
    for duplicate, sources in ((False, catalog.sites), (True, catalog.duplicates)):
        for site in sources:
            counts = overview.get(site.key, {})
            entries.append(
                {
                    "id": len(entries),
                    "key": site.key,
                    "name": site.name,
                    "url": site.url,
                    "country": site.country,
                    "language": site.language,
                    "duplicate": duplicate,
                    "mechanisms": mechanism_counts.get(
                        site.key, {"recent": 0, "supplement": 0, "review": 0}
                    ),
                    **{
                        k: counts.get(k, 0)
                        for k in (
                            "pages",
                            "read",
                            "confirmed",
                            "with_examples",
                            "pending",
                            "failures",
                        )
                    },
                }
            )
    unique = [entry for entry in entries if not entry["duplicate"]]
    return {
        "records": len(entries),
        "unique_sources": len(catalog.sites),
        "duplicate_records": len(catalog.duplicates),
        "sites": entries,
        "totals": {
            "with_recent_mechanisms": sum(bool(e["mechanisms"]["recent"]) for e in unique),
            "recent_mechanisms": sum(e["mechanisms"]["recent"] for e in unique),
            "with_usable_mechanisms": sum(
                bool(e["mechanisms"]["recent"] or e["mechanisms"]["supplement"]) for e in unique
            ),
            "without_recent_mechanisms": sum(not e["mechanisms"]["recent"] for e in unique),
            "with_sections": sum(bool(e["read"]) for e in unique),
            "with_examples": sum(bool(e["with_examples"]) for e in unique),
            "confirmed_sections": sum(e["confirmed"] for e in unique),
            "with_gaps": sum(bool(e["pending"] or e["failures"]) for e in unique),
        },
    }


def mapping_sections(
    store: StateStore,
    site: Site,
    *,
    mode: str,
    search: str,
    offset: int,
    limit: int,
    index: MechanismIndex | None = None,
) -> dict[str, Any]:
    """Paginação de seções; um exemplo é evidência histórica, não garantia de coleta futura."""
    total, rows = store.mapping_section_rows(
        site.key,
        mode=mode,
        search=search,
        offset=offset,
        limit=limit,
    )
    if index is None:
        index = MechanismIndex(site, store.mapping_sources(site.key))
    sections = []
    for row in rows:
        samples: Sequence[str | None] = row.pop("samples")
        checked = row["checked_at"]
        row["coverage"] = index.associate(
            MapPage(
                url=row["url"],
                found_on="",
                kind=row["kind"],
                status=PageStatus(row["status"]),
                page_type=row["page_type"],
                checked_at=checked,
                feeds=tuple(row.pop("feeds")),
                article_urls=tuple(u for u in samples if u),
            )
        )
        if isinstance(checked, datetime):
            row["checked_at"] = checked.isoformat()
        row["url"] = safe_site_url(site, row["url"])
        row["example_url"] = next(
            (safe for sample in samples if (safe := safe_site_url(site, sample)) is not None),
            None,
        )
        sections.append(row)
    return {
        "site": {**asdict(site), "key": site.key},
        "total": total,
        "offset": offset,
        "limit": limit,
        "mode": mode,
        "sections": sections,
    }


def mapping_mechanisms(
    store: StateStore,
    sites: Sequence[Site],
    *,
    rules: ManualRulesBook | None = None,
    mode: str = "all",
    search: str = "",
) -> list[dict[str, Any]]:
    """Lista XML auditado; aceita mecanismo fora do prefixo editorial, no host da fonte."""
    by_key = {site.key: site for site in sites}
    rows = store.mapping_mechanism_rows(sites[0].key if len(sites) == 1 else None)
    records = []
    for row in rows:
        site = by_key.get(row["site_key"])
        if site is None:
            continue
        record = mechanism_record(site, row, rules.for_site(site).exclude if rules else ())
        if record["group"] == "excluded":
            continue
        if mode != "all" and record["group"] != mode:
            continue
        if search.casefold() not in (row["url"] + " " + (row["kind"] or "")).casefold():
            continue
        records.append(record)
    return sorted(
        records,
        key=lambda r: (r["group"] != "recent", r["group"] != "supplement", r["partial"], r["url"]),
    )
