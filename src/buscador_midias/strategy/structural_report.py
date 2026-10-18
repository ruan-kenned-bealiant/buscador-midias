"""Balanço estrutural do inventário, independente de notícias e mecanismos de coleta."""

from collections.abc import Sequence
from dataclasses import dataclass

from buscador_midias.domain.mapping import MapPage, PageStatus
from buscador_midias.domain.site import Site
from buscador_midias.strategy.mapping_review import review_entry

EXAMINED = "navegação identificada examinada"
PARTIAL = "mapa parcial"
RESTRICTED = "acesso restrito"
UNCONFIRMED = "estrutura não confirmada"
_FAILURES = {PageStatus.ERROR, PageStatus.INVALID, PageStatus.BLOCKED, PageStatus.DISALLOWED}


@dataclass(frozen=True)
class StructuralSummary:
    """Conta páginas observadas; não estima as seções ainda desconhecidas do site."""

    site_key: str
    outcome: str
    reason: str
    pages: int
    sections_confirmed: int
    sections_read_unknown: int
    navigation_candidates: int
    read: int
    pending: int
    failures: int
    articles: int
    services: int
    taxonomies: int
    last_checked: str


def summarize_structure(site: Site, pages: Sequence[MapPage]) -> StructuralSummary:
    """Só considera examinada a estrutura com evidência e sem lacunas reconhecidas.

    Uma entrada lida, sozinha, não prova a estrutura. Falhas em subseções deixam o mapa
    parcial mesmo quando a fila de pendências está vazia. Restrição na entrada é explícita.
    """
    roots = [p for p in pages if p.kind == "root"]
    eligible = [p for p in pages if p.is_section_candidate or p.kind == "root"]
    read = [p for p in eligible if p.status is PageStatus.OK and site.owns(p.url)]
    sections = [p for p in eligible if p.kind != "root"]
    confirmed = sum(p.status is PageStatus.OK and p.page_type == "section" for p in sections)
    unknown = sum(p.status is PageStatus.OK and p.page_type == "unknown" for p in sections)
    navigation = sum(p.discovery_basis == "navigation" for p in sections)
    pending = sum(p.status is PageStatus.PENDING for p in eligible)
    failures = sum(p.status in _FAILURES for p in eligible)
    entry_ok = any(p.status is PageStatus.OK and p.is_section_candidate for p in roots)
    reviews = [r for p in roots if (r := review_entry(p)) is not None]
    restriction = any(
        p.status in (PageStatus.BLOCKED, PageStatus.DISALLOWED) for p in roots
    ) or any(r.category in (
        "robots: proibição explícita", "robots: leitura bloqueada", "acesso bloqueado",
        "limite de requisições",
    ) for r in reviews)
    observed_structure = confirmed > 0 or any(
        p.status is PageStatus.OK and p.discovery_basis == "navigation" for p in sections
    )
    outside = any(not site.owns(p.url) for p in eligible)
    if restriction:
        outcome = RESTRICTED
        reason = "; ".join(dict.fromkeys(r.category for r in reviews)) or "entrada bloqueada"
    elif not entry_ok or not observed_structure:
        outcome = UNCONFIRMED
        reason = "; ".join(dict.fromkeys(r.category for r in reviews)) or (
            "entrada/seções sem evidência estrutural suficiente"
        )
    elif pending or failures or outside:
        outcome = PARTIAL
        reason = f"{pending} candidatas pendentes; {failures} falhas/proibições"
        if outside:
            reason += "; endereço fora do escopo cadastrado"
    else:
        outcome = EXAMINED
        reason = "sem pendências ou falhas nas candidatas reconhecidas; não prova exaustividade"
    dates = [p.checked_at for p in pages if p.checked_at is not None]
    return StructuralSummary(
        site.key, outcome, reason, len(pages), confirmed, unknown, navigation, len(read),
        pending, failures, sum(p.page_type == "article" for p in pages),
        sum(p.page_type == "service" for p in pages),
        sum(p.page_type == "taxonomy" or p.kind == "taxonomy" for p in pages),
        max(dates).isoformat() if dates else "",
    )
