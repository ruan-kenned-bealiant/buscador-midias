"""Cria consulta funcional sem filtros de publicação, preservando todos os arquivos de origem."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lxml import etree

import consolidate_round12 as reports
from buscador_midias.dashboard.functional import count_xml
from buscador_midias.domain.site import Site
from buscador_midias.domain.url import canonicalize_url, url_hash
from buscador_midias.extraction.sample_audit import is_audio_destination
from buscador_midias.sites.loader import load_sites

ROOT = reports.ROOT
BASE = ROOT / "var/analise-rodada-12"
ROUND = reports.ROUND
REVISION = BASE / "resolucao-inconclusivos"
AUDIT = ROOT / "var/auditoria-dirigida-10"
AUDITS = (AUDIT, ROOT / "var/auditoria-dirigida-10-02", ROOT / "var/auditoria-final-problemas")
OUT = ROOT / "var/consulta-funcionais"


def rows(path: Path) -> list[dict[str, str]]:
    """Lê o CSV sem alterá-lo."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def identity(site: str, url: str) -> tuple[str, str]:
    """Agrupa o mesmo mecanismo sem duplicar aliases canônicos."""
    return site, canonicalize_url(url)


def cached_count(item: dict[str, Any]) -> dict[str, Any]:
    """Conta o XML direto já armazenado; nunca baixa ou expande outro documento."""
    candidates = [item["url"]]
    if item.get("final_url"):
        candidates.append(item["final_url"])
    caches = [
        *(
            p / "http-cache"
            for p in reversed(AUDITS)
            if (p / "execucao.json").exists()
            and reports.read_json(p / "execucao.json")["status"] == "completed"
        ),
        REVISION / "http-cache",
        ROUND / "cache-http-fila",
        ROOT / "var/validacao-final/cache",
        *(
            ROOT / "var" / name / "cache"
            for name in (
                "auditoria-acesso-rss",
                "auditoria-acesso-sitemaps",
                "revisao-web-fontes",
                "exploracao-pendentes-final",
                "conferencia-diretorio-g1",
            )
        ),
    ]
    for directory in caches:
        pending = list(candidates)
        seen: set[str] = set()
        while pending and len(seen) < 6:
            url = pending.pop(0)
            if url in seen:
                continue
            seen.add(url)
            digest = hashlib.sha256(url.encode()).hexdigest()
            meta_path, body_path = directory / (digest + ".json"), directory / (digest + ".gz")
            if meta_path.exists():
                meta = reports.read_json(meta_path)
                if meta.get("status") in (301, 302, 303, 307, 308) and meta.get("final_url"):
                    pending.append(meta["final_url"])
                if not meta.get("has_body") or not 200 <= (meta.get("status") or 0) < 300:
                    continue
            if not body_path.exists():
                continue
            try:
                with gzip.open(body_path, "rb") as stream:
                    body = stream.read(50 * 1024 * 1024 + 1)
                if len(body) > 50 * 1024 * 1024:
                    continue
                count = count_xml(body, url)
            except (ValueError, OverflowError, OSError, etree.XMLSyntaxError):
                continue
            return dict(
                **count,
                count_file=str(body_path.relative_to(ROOT)),
                count_scope="XML direto em cache, sem filtro de data",
            )
    return dict(
        kind=item["kind"],
        listed=item.get("listed_fallback"),
        children=item.get("children_fallback"),
        index=item.get("index_fallback", False),
        count_file="",
        count_scope="Resumo da verificação sem filtro de data"
        if item.get("listed_fallback") is not None
        else "Contagem sem data não registrada",
    )


def audit_summaries() -> dict[str, Any]:
    """Reúne o diagnóstico mais recente de cada fonte sem alterar as provas anteriores."""
    summaries = {}
    for directory in AUDITS:
        execution = directory / "execucao.json"
        if not execution.exists() or reports.read_json(execution)["status"] != "completed":
            continue
        for file in (directory / "fontes").glob("*.json"):
            data = reports.read_json(file)
            reasons = Counter(
                r["detalhe"] or f"HTTP {r['http_status']}"
                for r in data["records"]
                if not r["xml_valido"]
            )
            status = (
                "incomplete"
                if data.get("incomplete")
                else "text"
                if data["texts"] > 0
                else "xml"
                if data["functional"] > 0
                else "unconfirmed"
            )
            summaries[data["key"]] = dict(
                status=status,
                tested=data["xml_tested"],
                xml=data["functional"],
                texts=data["texts"],
                reasons=list(reasons),
                observed_at=data["completed_at"],
            )
    return summaries


def eligible_sample(site: Site, sample: dict[str, Any], alias: str | None = None) -> bool:
    """Exige artigo textual no escopo editorial ou no domínio oficial explicitamente verificado."""
    return bool(
        sample.get("texto_util_confirmado")
        and not sample.get("audio_dedicado")
        and not is_audio_destination(sample["url"])
        and (site.owns(sample["url"]) or alias and urlsplit(sample["url"]).hostname == alias)
    )


def main() -> None:
    """Gera outra visão, mantendo seleção e histórico da rodada 12 intactos."""
    OUT.mkdir(parents=True, exist_ok=True)
    latest = rows(BASE / "etapa-5/mecanismos.csv")
    catalog = load_sites(ROOT / "var/validacao-final/catalogo-congelado.csv")
    sites = {s.key: s for s in catalog.sites}
    historical_rows = rows(BASE / "etapa-4/mecanismos.csv")
    historical_text = {
        identity(r["fonte"], r["mecanismo"]): int(r["textos_confirmados"]) for r in historical_rows
    }
    historical_examples = {
        identity(r["fonte"], r["mecanismo"]): r["exemplo_textual"] for r in historical_rows
    }
    del historical_rows
    observations = {}
    for file in (REVISION / "resultados").glob("*.json"):
        observation = reports.read_json(file)
        observations[identity(observation["fonte"], observation["url"])] = observation
    for audit_directory in AUDITS:
        audit_execution = audit_directory / "execucao.json"
        if (
            not audit_execution.exists()
            or reports.read_json(audit_execution)["status"] != "completed"
        ):
            continue
        existing = {identity(r["fonte"], r["mecanismo"]) for r in latest}
        for file in sorted((audit_directory / "resultados").glob("*.json")):
            observation = reports.read_json(file)
            key = identity(observation["fonte"], observation["url"])
            observations[key] = observation
            if observation["xml_valido"] and key not in existing:
                latest.append(
                    dict(
                        fonte=observation["fonte"],
                        mecanismo=observation["url"],
                        xml_confirmado="True",
                        http_no_escopo="True",
                        tipo_reutilizavel=observation["tipo"],
                        textos_confirmados="0",
                        exemplo_textual="",
                        canal_podcast_comprovado="False",
                    )
                )
                existing.add(key)
    memberships: dict[tuple[str, str], set[str]] = defaultdict(set)
    with sqlite3.connect(f"file:{ROUND / 'fila-documentos.sqlite'}?mode=ro", uri=True) as queue:
        roots = {r[0]: identity(r[1], r[2]) for r in queue.execute("SELECT id,site,url FROM roots")}
        for root_id, digest in queue.execute("SELECT root_id,hash FROM evidence"):
            memberships[roots[root_id]].add(digest)
    original_news: dict[str, set[str]] = defaultdict(set)
    with (ROUND / "noticias-48h.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            digest = url_hash(row["url"])
            original_news[row["site_key"]].add(digest)
            if row["source_sitemap"].startswith(("http://", "https://")):
                memberships[identity(row["site_key"], row["source_sitemap"])].add(digest)
    traced_roots = set(roots.values())
    items: list[dict[str, Any]] = []
    historical_failures = 0
    for row in latest:
        key = identity(row["fonte"], row["mecanismo"])
        observation = observations.get(key)
        limited = bool(
            observation
            and observation.get("detalhe")
            in ("body_or_time_limit", "audit_budget_reached", "cached_response_unavailable")
        )
        functional = (
            row["xml_confirmado"] == "True"
            if limited
            else observation["xml_valido"] and observation.get("uso_noticias", True)
            if observation
            else row["xml_confirmado"] == "True"
        )
        if (
            observation
            and not limited
            and not observation["xml_valido"]
            and row["xml_confirmado"] == "True"
        ):
            historical_failures += 1
        if not functional or row["http_no_escopo"] != "True":
            continue
        kind = (
            (observation["tipo"] or row["tipo_reutilizavel"])
            if observation
            else row["tipo_reutilizavel"]
        )
        if kind not in ("feed", "sitemap", "news_sitemap"):
            continue
        historical_example = historical_examples.get(key, "")
        safe_historical = (
            historical_text.get(key, 0)
            if historical_example
            and eligible_sample(
                sites[row["fonte"]], dict(url=historical_example, texto_util_confirmado=True)
            )
            else 0
        )
        verified = (
            [
                a
                for a in observation["amostras"]
                if eligible_sample(sites[row["fonte"]], a, observation.get("alias_oficial"))
            ]
            if observation
            else []
        )
        items.append(
            dict(
                site=row["fonte"],
                url=row["mecanismo"],
                kind="feed" if kind == "feed" else "sitemap",
                round_news=len(memberships[key])
                if key in traced_roots or memberships[key]
                else None,
                texts=max(safe_historical, len(verified)),
                example=verified[0]["url"]
                if verified
                else historical_example
                if safe_historical
                else "",
                audio=row["canal_podcast_comprovado"] == "True"
                or bool(
                    observation
                    and observation.get("podcast_xml")
                    and observation["amostras"]
                    and all(
                        a.get("audio_dedicado") and not a.get("texto_util_confirmado")
                        for a in observation["amostras"]
                    )
                ),
                review_limited=limited,
                article_blocked=bool(
                    observation
                    and observation["amostras"]
                    and all(
                        a.get("http_status") in (401, 403, 429) for a in observation["amostras"]
                    )
                ),
                observed_at=observation["observado_em"] if observation else "",
                final_url=observation.get("final_url", "") if observation else "",
                listed_fallback=observation["candidatas"] if observation and not limited else None,
                children_fallback=observation["filhos_listados"]
                if observation and not limited
                else None,
                index_fallback=bool(observation and observation["filhos_listados"]),
            )
        )
    print(f"Contando XMLs salvos de {len(items)} mecanismos funcionais...", flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        counts = list(pool.map(cached_count, items))
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_hashes: dict[str, set[str]] = defaultdict(set)
    for item, count in zip(items, counts, strict=True):
        item.update(count)
        item["evidence"] = "audio" if item["audio"] else "text" if item["texts"] > 0 else "xml"
        source_hashes[item["site"]].update(memberships[identity(item["site"], item["url"])])
        for field in ["final_url", "listed_fallback", "children_fallback", "index_fallback"]:
            item.pop(field, None)
        by_source[item["site"]].append(item)
    source_rows = []
    for site in catalog.sites:
        values = by_source[site.key]
        source_rows.append(
            dict(
                key=site.key,
                name=site.name,
                country=site.country,
                functional=len(values),
                rss=sum(v["kind"] == "feed" for v in values),
                sitemaps=sum(v["kind"] == "sitemap" for v in values),
                text_mechanisms=sum(v["texts"] > 0 and not v["audio"] for v in values),
                round_collected=len(original_news[site.key]),
                round_attributed=len(source_hashes[site.key]),
                round_unattributed=len(original_news[site.key] - source_hashes[site.key]),
            )
        )
        values.sort(key=lambda r: (r["kind"] != "feed", -(r["round_news"] or 0), r["url"]))
    execution = reports.read_json(ROUND / "execucao.json")
    criteria = [
        "Funcional: RSS/sitemap interpretado, independentemente de data e de haver notícias novas.",
        "A última revisão explícita prevalece sobre o sucesso histórico do XML.",
        "Limites próprios da revisão não invalidam XML funcional comprovado no histórico.",
        "XML funcional não garante acesso ao texto; bloqueios nos artigos aparecem separados.",
        "URLs listadas são contadas no documento direto salvo, sem filtro de publicação; "
        "índices mostram filhos separadamente, sem contá-los como notícias.",
        "Encontradas na rodada 12: URLs candidatas distintas por mecanismo, incluindo descendentes "
        "registrados, na janela de 48h daquela coleta. Não é o total de todo o arquivo do site.",
        "Sem vínculo histórico individual, notícias de origem sintética ficam sem atribuição.",
        "Amostras novas não alteram a procedência histórica nem os totais da rodada 12.",
        "Totais por mecanismo podem se sobrepor; por fonte, as URLs atribuídas são deduplicadas.",
        "Texto confirmado é evidência amostral; nem toda URL listada é notícia textual validada.",
        "Áudio é identificado mesmo com XML funcional. Esta entrega não faz nova coleta.",
        "Contagem não registrada é desconhecida, nunca convertida em zero.",
        "Feeds de teste comprovados permanecem no relatório e fora dos links para notícias.",
    ]
    meta = dict(
        generated_at=datetime.now(UTC).isoformat(),
        sources=len(source_rows),
        sources_functional=sum(s["functional"] > 0 for s in source_rows),
        sources_with_text=sum(s["text_mechanisms"] > 0 for s in source_rows),
        text_mechanisms=sum(i["texts"] > 0 and not i["audio"] for i in items),
        unknown_listed=sum(i["listed"] is None for i in items),
        unknown_round=sum(i["round_news"] is None for i in items),
        functional=len(items),
        rss=sum(i["kind"] == "feed" for i in items),
        sitemaps=sum(i["kind"] == "sitemap" for i in items),
        window_since=execution["window_since"],
        window_until=execution["window_until"],
        counts_without_date=sum(i["listed"] is not None for i in items),
        historical_with_latest_failure=historical_failures,
        criteria=criteria,
    )
    assert len(source_rows) == 1294
    assert len({identity(i["site"], i["url"]) for i in items}) == len(items)
    assert all(i["listed"] == 0 for i in items if i["index"] and i["listed"] is not None)
    snapshot = dict(
        meta=meta, sources=source_rows, mechanisms=dict(by_source), audits=audit_summaries()
    )
    temp = OUT / "retrato.tmp"
    temp.write_text(json.dumps(snapshot, ensure_ascii=False) + "\n")
    temp.replace(OUT / "retrato.json")
    reports.OUTPUT = OUT
    reports.write_csv("fontes.csv", source_rows)
    reports.write_csv("funcionais.csv", items)
    reports.write_csv(
        "links-com-texto.csv", [r for r in items if r["texts"] > 0 and not r["audio"]]
    )
    reports.workbook(
        {
            "Fontes": source_rows,
            "RSS e sitemaps": items,
            "Critérios": [dict(criterio=s) for s in criteria],
        }
    )
    (OUT / "consolidacao.xlsx").replace(OUT / "fontes-funcionais.xlsx")
    audit_reports = [
        directory / "relatorio.md"
        for directory in AUDITS
        if (directory / "relatorio.md").exists()
        and reports.read_json(directory / "execucao.json")["status"] == "completed"
    ]
    if audit_reports:
        (OUT / "auditoria-dirigida.md").write_text(
            "\n\n---\n\n".join(p.read_text() for p in audit_reports), encoding="utf-8"
        )
    preservation = {}
    paths = [
        ROUND / "fila-documentos.sqlite",
        ROUND / "noticias-48h.csv",
        ROUND / "plano-todos.json",
        ROUND / "encerramento.json",
    ]
    for directory in [BASE / f"etapa-{i}" for i in range(1, 6)]:
        paths.extend(
            p for p in directory.iterdir() if p.suffix in (".json", ".csv", ".xlsx", ".md")
        )
    paths.extend([REVISION / "plano.json", REVISION / "resumo.json"])
    for path in paths:
        with path.open("rb") as hash_stream:
            preservation[str(path.relative_to(ROOT))] = hashlib.file_digest(
                hash_stream, "sha256"
            ).hexdigest()
    (OUT / "historico-preservado.json").write_text(
        json.dumps(
            dict(
                generated_at=meta["generated_at"],
                modified_inputs=False,
                sha256=preservation,
                retained_directories=[str(ROUND.relative_to(ROOT)), str(BASE.relative_to(ROOT))],
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps({k: v for k, v in meta.items() if k != "criteria"}, ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()
