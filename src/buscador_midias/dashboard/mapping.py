"""Consulta do mapa estrutural e de amostras observadas, sem chamadas aos sites."""

from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from buscador_midias.domain.site import Site
from buscador_midias.sites.loader import LoadResult
from buscador_midias.state.store import StateStore


def safe_site_url(site: Site, value: str | None) -> str | None:
    """Só libera links HTTP do escopo da fonte para a tela."""
    if not value:
        return None
    try:
        return value if urlsplit(value).scheme in ("http", "https") and site.owns(value) else None
    except ValueError:
        return None


def mapping_catalog(store: StateStore, catalog: LoadResult) -> dict[str, Any]:
    """As 1.299 linhas do catálogo, com duplicatas explícitas e contagens por fonte única."""
    overview = store.mapping_overview()
    entries: list[dict[str, Any]] = []
    for duplicate, sources in ((False, catalog.sites), (True, catalog.duplicates)):
        for site in sources:
            counts = overview.get(site.key, {})
            entries.append({
                "id": len(entries), "key": site.key, "name": site.name, "url": site.url,
                "country": site.country, "language": site.language, "duplicate": duplicate,
                **{k: counts.get(k, 0) for k in (
                    "pages", "read", "confirmed", "with_examples", "pending", "failures",
                )},
            })
    unique = [entry for entry in entries if not entry["duplicate"]]
    return {
        "records": len(entries), "unique_sources": len(catalog.sites),
        "duplicate_records": len(catalog.duplicates), "sites": entries,
        "totals": {
            "with_sections": sum(bool(e["read"]) for e in unique),
            "with_examples": sum(bool(e["with_examples"]) for e in unique),
            "confirmed_sections": sum(e["confirmed"] for e in unique),
            "with_gaps": sum(bool(e["pending"] or e["failures"]) for e in unique),
        },
    }


def mapping_sections(
    store: StateStore, site: Site, *, mode: str, search: str, offset: int, limit: int,
) -> dict[str, Any]:
    """Paginação de seções; um exemplo é evidência histórica, não garantia de coleta futura."""
    total, rows = store.mapping_section_rows(
        site.key, mode=mode, search=search, offset=offset, limit=limit,
    )
    sections = []
    for row in rows:
        samples: Sequence[str | None] = row.pop("samples")
        checked = row["checked_at"]
        if isinstance(checked, datetime):
            row["checked_at"] = checked.isoformat()
        row["url"] = safe_site_url(site, row["url"])
        row["example_url"] = next(
            (safe for sample in samples if (safe := safe_site_url(site, sample)) is not None), None,
        )
        sections.append(row)
    return {"site": {**asdict(site), "key": site.key}, "total": total, "offset": offset,
            "limit": limit, "mode": mode, "sections": sections}
