"""Prioridades de complementação calculadas dos checkpoints, sem novas buscas."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from buscador_midias.domain.mapping import MapPage, MapSource, PageStatus
from buscador_midias.domain.site import Site
from buscador_midias.strategy.mapping_coverage import CoverageIndex
from buscador_midias.strategy.mapping_export import Cell, Sheet

HEADERS = (
    "Prioridade",
    "Chave",
    "Fonte",
    "Situação",
    "Próxima ação",
    "Páginas lidas",
    "Seções com amostra",
    "Amostras cobertas",
    "Amostras parciais",
    "Amostras sem correspondência",
    "Links da amostra",
    "Links encontrados",
    "Mecanismos recentes no escopo",
    "Mecanismos sem data no escopo",
    "Pendências estruturais",
    "Seções confirmadas por metadados",
)


def priority_row(
    site: Site,
    pages: Sequence[MapPage],
    checks: Sequence[MapSource],
    *,
    now: datetime,
) -> tuple[Cell, ...]:
    """Ordena lacunas observadas; uma amostra correspondente não comprova cobertura total."""
    eligible = [p for p in pages if p.is_section_candidate]
    read = sum(p.status is PageStatus.OK for p in eligible)
    index = CoverageIndex(checks)
    measured = [index.compare(p) for p in eligible]
    sampled = [m for m in measured if m.sampled]
    full = sum(m.status == "sample_full" for m in sampled)
    partial = sum(m.status == "partial" for m in sampled)
    missing = sum(m.status == "no_match" for m in sampled)
    current = [
        c
        for c in checks
        if c.kind in ("feed", "sitemap", "news_sitemap")
        and c.checked_at >= now - timedelta(days=1)
        and any(site.owns(url) for url in c.listed_urls)
    ]
    recent = sum(c.verdict in ("fresh", "slow") for c in current)
    undated = sum(c.verdict == "undated" for c in current)
    if not read:
        blocked = any(
            p.kind == "root"
            and (
                p.status in (PageStatus.BLOCKED, PageStatus.DISALLOWED)
                or "robots" in p.detail.lower()
            )
            for p in pages
        )
        priority, state, action = (
            3,
            "acesso bloqueado" if blocked else "estrutura sem leitura válida",
            "Revisar entrada, acesso e política; retentativa somente com orçamento definido.",
        )
    elif not recent:
        priority, state, action = (
            1,
            "sem mecanismo recente confirmado",
            "Conferir mecanismos das editorias; validar datas nos feeds sem data.",
        )
    elif not sampled or full != len(sampled):
        priority, state, action = (
            2,
            "lacunas ou associação por seção pendente",
            "Conferir Lacunas e Complementos; priorizar navegação editorial e ganho na amostra.",
        )
    else:
        priority, state, action = (
            4,
            "amostras correspondentes",
            "Validar descoberta de notícias em piloto; manter limitações e seções sem amostra.",
        )
    return (
        priority,
        site.key,
        site.name,
        state,
        action,
        read,
        len(sampled),
        full,
        partial,
        missing,
        sum(m.sampled for m in sampled),
        sum(m.matched for m in sampled),
        recent,
        undated,
        sum(p.status is PageStatus.PENDING for p in pages),
        sum(p.page_type == "section" for p in pages),
    )


def priority_table(rows: Sequence[tuple[Cell, ...]]) -> dict[str, Sheet]:
    """Entrega uma tabela curta, ordenada por urgência e chave da fonte."""
    return {"Prioridades": (HEADERS, sorted(rows, key=lambda row: (row[0] or 0, str(row[1]))))}
