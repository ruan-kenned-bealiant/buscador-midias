"""Inventário reutilizável de RSS/sitemaps e associação por evidência armazenada.

Nenhuma função faz HTTP ou altera estratégias. A associação compara até cinco links
por página; correspondência histórica nunca é promovida a cobertura atual completa.
"""

from __future__ import annotations

import csv
import gzip
import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from buscador_midias.domain.dated_url import find_date
from buscador_midias.domain.mapping import MapPage, MapSource, PageStatus
from buscador_midias.domain.site import Site
from buscador_midias.domain.url import canonicalize_url
from buscador_midias.state.store import StateStore
from buscador_midias.strategy.hints import _is_signed
from buscador_midias.strategy.manual import ManualRulesBook, is_excluded
from buscador_midias.strategy.mapping_coverage import normalize_source_audits

XML_KINDS = frozenset({"feed", "sitemap", "news_sitemap"})
READABLE = frozenset({"fresh", "slow", "undated", "stale", "archive", "empty"})


def safe_mechanism_url(site: Site, url: str) -> bool:
    """Aceita mecanismos do host, inclusive fora do prefixo editorial da fonte."""
    try:
        parts = urlsplit(url)
        return (
            site.owns_host(url)
            and not parts.username
            and not parts.password
            and not _is_signed(url)
        )
    except ValueError:
        return False


def mechanism_record(
    site: Site,
    row: dict[str, Any],
    exclude: Sequence[str] = (),
) -> dict[str, Any]:
    """Classifica utilidade sem confundir XML legível com atualização recente."""
    result = {k: v for k, v in row.items() if k != "samples"}
    url = row["url"]
    safe = safe_mechanism_url(site, url)
    group, reason = "review", "Conteúdo antigo, vazio ou sem leitura XML válida"
    if not safe or is_excluded(url, exclude) or row["kind"] not in XML_KINDS:
        group, reason = "excluded", "Fora do escopo, excluído manualmente ou não é RSS/sitemap"
    elif find_date(url) is not None:
        reason = "URL com data fixa: preservar como histórico; exige índice ou modelo validado"
    elif row["verdict"] == "fresh" and row["listed_count"]:
        group, reason = "recent", "Conteúdo recente na última auditoria; revalidar na busca"
    elif row["verdict"] in {"slow", "undated"} and row["listed_count"]:
        group, reason = "supplement", "XML com itens, mas pouca atualização ou sem datas"
    result.update(group=group, reason=reason, url=url if safe else None)
    checked = result["checked_at"]
    if isinstance(checked, datetime):
        result["checked_at"] = checked.isoformat()
    result["example_url"] = next((u for u in row.get("samples", ()) if u and site.owns(u)), None)
    return result


class MechanismIndex:
    """Índice invertido: encontra mecanismos pelos links observados, sem novas buscas."""

    def __init__(
        self,
        site: Site,
        sources: Sequence[MapSource],
        exclude: Sequence[str] = (),
    ) -> None:
        """Indexa somente XML interpretado, respeitando escopo e exclusões manuais."""
        self.site = site
        self.sources: dict[str, MapSource] = {}
        self.links: dict[str, set[str]] = defaultdict(set)
        for source in normalize_source_audits(sources):
            if (
                source.kind not in XML_KINDS
                or source.verdict not in READABLE
                or not safe_mechanism_url(site, source.url)
                or is_excluded(source.url, exclude)
            ):
                continue
            self.sources[source.url] = source
            for url in source.listed_urls:
                if site.owns(url):
                    self.links[canonicalize_url(url)].add(source.url)

    def associate(self, page: MapPage) -> dict[str, Any]:
        """Separa links declarados, correspondência e defasagem entre as leituras."""
        sample = {canonicalize_url(u) for u in page.article_urls if self.site.owns(u)}
        matches: dict[str, set[str]] = defaultdict(set)
        for url in sample:
            for source_url in self.links.get(url, ()):
                matches[source_url].add(url)
        declared = set(page.feeds)
        found = []
        for url in sorted(set(matches) | (declared & self.sources.keys())):
            source = self.sources[url]
            after = bool(page.checked_at and source.checked_at >= page.checked_at)
            row = mechanism_record(
                self.site,
                {
                    "url": url,
                    "kind": source.kind,
                    "verdict": source.verdict,
                    "checked_at": source.checked_at,
                    "partial": source.partial,
                    "detail": source.detail,
                    "listed_count": len(source.listed_urls),
                },
            )
            row.update(
                matched=len(matches[url]),
                declared=url in declared,
                evidence="sample_match" if matches[url] else "declared",
                audit_after_page=after,
            )
            found.append(row)
        current = set().union(*(matches[r["url"]] for r in found if r["audit_after_page"]))
        historical = set().union(*matches.values())
        if page.status is not PageStatus.OK:
            status = "page_not_read"
        elif not sample:
            status = "no_sample"
        elif current:
            status = "sample_full" if current == sample else "sample_partial"
        elif historical:
            status = "historical_match"
        else:
            status = "no_match"
        return {
            "status": status,
            "sampled": len(sample),
            "matched": len(current),
            "historical_matched": len(historical),
            "mechanisms": found,
            "unvalidated_declared": sorted(
                u for u in declared - self.sources.keys() if safe_mechanism_url(self.site, u)
            ),
        }


def export_mechanisms(
    store: StateStore,
    sites: Sequence[Site],
    rules: ManualRulesBook,
    out: Path,
) -> dict[str, Any]:
    """Exporta lista utilizável, plano explícito e associações, em uma passagem finita."""
    out.mkdir(parents=True, exist_ok=True)
    by_key = {s.key: s for s in sites}
    metadata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in store.mapping_mechanism_rows():
        site = by_key.get(row["site_key"])
        if site:
            metadata[site.key].append(mechanism_record(site, row, rules.for_site(site).exclude))
    mechanism_fields = (
        "site_key",
        "url",
        "kind",
        "verdict",
        "group",
        "reason",
        "checked_at",
        "partial",
        "listed_count",
        "example_url",
        "detail",
    )
    summary: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sources": len(sites),
        "http_requests": 0,
    }
    counts: Counter[str] = Counter()
    plans = []
    expanded_plans = []
    with (
        (out / "mecanismos.csv").open("w", encoding="utf-8-sig", newline="") as all_file,
        (out / "hints-recentes.csv").open("w", encoding="utf-8", newline="") as hint_file,
        (out / "fontes.csv").open("w", encoding="utf-8-sig", newline="") as site_file,
        gzip.open(out / "secoes-mecanismos.csv.gz", "wt", encoding="utf-8", newline="") as links,
        gzip.open(out / "lacunas-secoes.csv.gz", "wt", encoding="utf-8", newline="") as gaps,
    ):
        all_writer = csv.DictWriter(all_file, mechanism_fields, extrasaction="ignore")
        all_writer.writeheader()
        hints = csv.writer(hint_file)
        hints.writerow(("dominio", "news_sitemap_url", "rss_url", "sitemap_url"))
        source_writer = csv.writer(site_file)
        source_writer.writerow(
            (
                "chave",
                "nome",
                "pais",
                "recentes",
                "complementares",
                "revisar",
                "paginas",
                "paginas_com_correspondencia",
                "lacunas",
            )
        )
        link_writer = csv.writer(links)
        link_writer.writerow(
            (
                "chave",
                "pagina",
                "titulo",
                "situacao_amostra",
                "amostrados",
                "mecanismo",
                "tipo",
                "resultado",
                "grupo",
                "correspondencias",
                "declarado_na_pagina",
                "mecanismo_lido_em",
                "pagina_lida_em",
                "auditoria_posterior_pagina",
                "leitura_parcial",
            )
        )
        gap_writer = csv.writer(gaps)
        gap_writer.writerow(
            (
                "chave",
                "pagina",
                "titulo",
                "situacao",
                "amostrados",
                "encontrados",
                "historicos",
                "declarados_nao_validados",
            )
        )
        for site in sites:
            selected: dict[str, dict[str, Any]] = {}
            records = metadata[site.key]
            recent = [r for r in records if r["group"] == "recent"]
            counts["sources_with_recent"] += bool(recent)
            counts["sources_with_supplement"] += any(r["group"] == "supplement" for r in records)
            for row in records:
                if row["url"] is not None and row["group"] != "excluded":
                    all_writer.writerow(row)
                    counts["mechanisms_" + row["group"]] += 1
                if row["group"] == "recent":
                    normalized = canonicalize_url(row["url"])
                    if normalized not in selected:
                        selected[normalized] = row
            force = rules.for_site(site).force
            ordered = sorted(
                selected.values(),
                key=lambda r: (
                    r["url"] not in force,
                    r["partial"],
                    r["kind"] != "news_sitemap",
                    -r["listed_count"],
                    r["url"],
                ),
            )
            for row in ordered:
                cells = {"feed": 2, "sitemap": 3, "news_sitemap": 1}
                hint = [site.key, "", "", ""]
                hint[cells[row["kind"]]] = row["url"]
                hints.writerow(hint)
            if ordered and (not force or ordered[0]["url"] in force):
                plans.append(
                    {
                        "chave": site.key,
                        "mecanismos": [
                            {
                                "url": row["url"],
                                "kind": row["kind"],
                                "validado_em_utc": row["checked_at"],
                                "leitura_parcial": row["partial"],
                            }
                            for row in ordered
                        ],
                    }
                )
            elif ordered:
                counts["sources_omitted_from_plan_manual_force"] += 1
            index = MechanismIndex(
                site, store.mapping_sources(site.key), rules.for_site(site).exclude
            )
            pages = store.mapping_inventory_pages(site.key)
            associated = missing = 0
            for page in pages:
                result = index.associate(page)
                counts["sections_" + result["status"]] += 1
                matched = any(r["matched"] for r in result["mechanisms"])
                associated += matched
                for member in result["mechanisms"]:
                    link_writer.writerow(
                        (
                            site.key,
                            page.url,
                            page.title,
                            result["status"],
                            result["sampled"],
                            member["url"],
                            member["kind"],
                            member["verdict"],
                            member["group"],
                            member["matched"],
                            member["declared"],
                            member["checked_at"],
                            page.checked_at.isoformat() if page.checked_at else "",
                            member["audit_after_page"],
                            member["partial"],
                        )
                    )
                    counts["section_mechanism_links"] += 1
                if result["status"] != "sample_full":
                    missing += 1
                    gap_writer.writerow(
                        (
                            site.key,
                            page.url,
                            page.title,
                            result["status"],
                            result["sampled"],
                            result["matched"],
                            result["historical_matched"],
                            json.dumps(result["unvalidated_declared"], ensure_ascii=False),
                        )
                    )
            source_writer.writerow(
                (
                    site.key,
                    site.name,
                    site.country,
                    len(recent),
                    sum(r["group"] == "supplement" for r in records),
                    sum(r["group"] == "review" for r in records),
                    len(pages),
                    associated,
                    missing,
                )
            )
    # Plano ampliado separado: os XML lentos/sem data não viram "recentes".
    with (out / "hints-complementares.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("dominio", "news_sitemap_url", "rss_url", "sitemap_url"))
        for site in sites:
            force = rules.for_site(site).force
            candidates = sorted(
                (r for r in metadata[site.key] if r["group"] in {"recent", "supplement"}),
                key=lambda r: (
                    r["url"] not in force,
                    r["group"] != "recent",
                    r["partial"],
                    -r["listed_count"],
                    r["url"],
                ),
            )
            unique: dict[str, dict[str, Any]] = {}
            for record in candidates:
                unique.setdefault(canonicalize_url(record["url"]), record)
            selected_rows = list(unique.values())
            for record in selected_rows:
                if record["group"] == "supplement":
                    hint_cells = [site.key, "", "", ""]
                    hint_cells[{"news_sitemap": 1, "feed": 2, "sitemap": 3}[record["kind"]]] = (
                        record["url"]
                    )
                    writer.writerow(hint_cells)
            if selected_rows and (not force or selected_rows[0]["url"] in force):
                expanded_plans.append(
                    {
                        "chave": site.key,
                        "mecanismos": [
                            {
                                "url": r["url"],
                                "kind": r["kind"],
                                "resultado_auditoria": r["verdict"],
                                "validado_em_utc": r["checked_at"],
                                "leitura_parcial": r["partial"],
                            }
                            for r in selected_rows
                        ],
                    }
                )
    (out / "plano-ampliado.json").write_text(
        json.dumps(expanded_plans, ensure_ascii=False, indent=2)
    )
    summary["expanded_plan_sources"] = len(expanded_plans)
    summary["expanded_plan_mechanisms"] = sum(len(p["mecanismos"]) for p in expanded_plans)
    summary.update(counts)
    summary["plan_sources"] = len(plans)
    summary["plan_mechanisms"] = sum(len(p["mecanismos"]) for p in plans)
    (out / "plano-descoberta.json").write_text(json.dumps(plans, ensure_ascii=False, indent=2))
    (out / "resumo.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    (out / "LEIA-ME.txt").write_text(
        "Inventário offline; não foram feitas novas requisições.\n"
        "plano-descoberta.json: entrada explícita para --discovery-plan; somente recentes, "
        "respeitando exclusões/forçamentos. Não altera o plano padrão.\n"
        "hints-recentes.csv: entrada para --hints; dicas sujeitas à validação na busca.\n"
        "plano-ampliado.json e hints-complementares.csv: incluem também XML lento/sem data, "
        "explicitamente identificado; esses mecanismos não têm atualização recente comprovada.\n"
        "mecanismos.csv: XML recente, complementar e a revisar. URLs com data fixa ficam "
        "a revisar; não foram inventados modelos futuros.\n"
        "secoes-mecanismos.csv.gz: declaração na página e/ou correspondência de até cinco "
        "links. Datas diferentes são explícitas; vínculo histórico não é cobertura atual.\n"
        "lacunas-secoes.csv.gz: todas as páginas sem correspondência integral da amostra "
        "em auditoria posterior. Uma amostra integral não prova cobertura total.\n"
        "Leituras parciais permanecem identificadas. Conteúdo recente refere-se à data "
        "da auditoria; disponibilidade e atualidade precisam ser verificadas na próxima busca.\n"
    )
    return summary
