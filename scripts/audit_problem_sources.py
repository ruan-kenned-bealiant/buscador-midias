"""Diagnóstico finito por fonte, com limites, checkpoints e histórico separado."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from lxml import etree, html

import consolidate_round12 as reports
import validate_round12_samples as samples
from buscador_midias.discovery.feeds import find_feed_links
from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.discovery.xml_review import review_xml
from buscador_midias.extraction.reliability import PageSnapshot
from buscador_midias.extraction.sample_audit import audit_page
from buscador_midias.net.fetcher import FetchResult
from buscador_midias.sites.loader import load_sites

OUT = reports.ROOT / "var/auditoria-dirigida-10"
KEYS = (
    "band.com.br",
    "canaltech.com.br",
    "infoamazonia.org",
    "azmina.com.br",
    "agenciamural.org.br",
    "jornaldebrasilia.com.br",
    "maisgoias.com.br",
    "plural.jor.br",
    "bemparana.com.br",
    "itatiaia.com.br",
)

BATCH_TWO = (
    "mongabay.com.br",
    "acritica.com",
    "opopular.com.br",
    "jornalopcao.com.br",
    "gazetadigital.com.br",
    "hojeemdia.com.br",
    "cidadeverde.com",
    "180graus.com",
    "novonoticias.com",
    "diregional.com.br",
)

_RAW_LOCKS: defaultdict[str, Any] = defaultdict(threading.Lock)
ALL_PROBLEMS = False
PRIOR_AUDITS = (
    reports.ROOT / "var/auditoria-dirigida-10",
    reports.ROOT / "var/auditoria-dirigida-10-02",
)


def remaining_jobs(snapshot: dict[str, Any], records: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Congela três grupos disjuntos; nenhuma fonte já auditada entra novamente."""
    done = {
        j["key"]
        for directory in PRIOR_AUDITS
        for j in reports.read_json(directory / "plano.json")["jobs"]
    }
    by_site: dict[str, list[dict[str, str]]] = {}
    for row in records:
        by_site.setdefault(row["fonte"], []).append(row)
    jobs = []
    for source in snapshot["sources"]:
        key = source["key"]
        if key in done:
            continue
        mechanisms = snapshot["mechanisms"].get(key, [])
        inconclusive = [
            r
            for r in by_site.get(key, [])
            if r["classificacao"] == "inconclusivo" and r["http_no_escopo"] == "True"
        ]
        blocked = any(m["article_blocked"] for m in mechanisms)
        if not source["functional"]:
            group = 1
        elif blocked or not any(m["texts"] for m in mechanisms):
            group = 2
        elif inconclusive:
            group = 3
        else:
            continue
        functional = sorted(
            mechanisms, key=lambda m: (not m["article_blocked"], m["index"], -(m["listed"] or 0))
        )
        candidates = sorted(inconclusive, key=lambda r: -int(r["textos_confirmados"]))
        preferred = [m["url"] for m in functional[:4]] if group == 2 else []
        preferred += [r["mecanismo"] for r in candidates[:3]] if group != 1 else []
        jobs.append(dict(key=key, group=group, preferred=preferred))
    return sorted(jobs, key=lambda j: (j["group"], j["key"]))


class BudgetReader(samples.ScopedReader):
    """Limita requisições reais e tempo por fonte, inclusive robots e redirects."""

    def __init__(self, client: httpx.Client) -> None:
        """Inicia um orçamento por fonte, sem compartilhar limites entre sites."""
        super().__init__(client)
        self.started = time.monotonic()

    def raw(self, url: str) -> FetchResult:
        """Conta inclusive políticas e saltos antes de executar HTTP direto."""
        if self.requests >= 60 or time.monotonic() - self.started > 180:
            return FetchResult(url, None, b"", "audit_budget_reached")
        with _RAW_LOCKS[url]:
            return super().raw(url)


def prepare() -> dict[str, Any]:
    """Congela a lista; não adiciona fontes durante a execução."""
    file = OUT / "plano.json"
    if file.exists():
        return cast(dict[str, Any], reports.read_json(file))
    snapshot = reports.read_json(reports.ROOT / "var/consulta-funcionais/retrato.json")
    sites = {
        s.key: s
        for s in load_sites(reports.ROOT / "var/validacao-final/catalogo-congelado.csv").sites
    }
    with (reports.ROOT / "var/analise-rodada-12/etapa-5/mecanismos.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        records = list(csv.DictReader(stream))
    contexts: list[dict[str, Any]] = (
        remaining_jobs(snapshot, records) if ALL_PROBLEMS else [dict(key=k) for k in KEYS]
    )
    jobs = []
    for context in contexts:
        key = context["key"]
        base = sites[key].url.rstrip("/")
        urls = [
            r["mecanismo"]
            for r in records
            if r["fonte"] == key and r["mecanismo"].startswith("https://")
        ]
        # Priorizar os feeds gerais, evitando consumir todo o orçamento em editorias.
        defaults = [
            base + path
            for path in (
                "/feed/",
                "/sitemap.xml",
                "/sitemap_index.xml",
                "/wp-sitemap.xml",
                "/sitemap-news.xml",
            )
        ]
        extras = [base + "/?feed=rss2", base + "/rss.xml"]
        if key == "canaltech.com.br":
            defaults = [
                base + "/rss/",
                base + "/smap/sitemap.xml",
                base + "/smap/sitemap-news.xml",
            ] + defaults
        if key == "infoamazonia.org":
            extras += [base + "/en/category/topics/feed/"]
        jobs.append(
            dict(
                key=key,
                name=sites[key].name,
                base=base,
                before=next(s for s in snapshot["sources"] if s["key"] == key),
                seeds=list(dict.fromkeys(context.get("preferred", []) + defaults + urls + extras))[
                    :10
                ],
                group=context.get("group", 1),
            )
        )
    plan = dict(
        created_at=datetime.now(UTC).isoformat(),
        jobs=jobs,
        max_xml_per_source=12,
        max_articles_per_source=4,
        max_requests_per_source=60,
        max_seconds_per_source=180,
        max_depth=2,
        workers=12 if ALL_PROBLEMS else 4,
        retries=0,
        paid_services=False,
        publication_filter=False,
    )
    samples.save(file, plan)
    return plan


def audit(job: dict[str, Any], site: Any) -> dict[str, Any]:
    """Lê declarações oficiais e até 12 mecanismos; uma amostra de até quatro artigos."""
    target = OUT / "fontes" / (site.key.replace("/", "__") + ".json")
    if target.exists():
        return cast(dict[str, Any], reports.read_json(target))
    with httpx.Client(timeout=6, follow_redirects=False, trust_env=False) as client:
        reader = BudgetReader(client)
        home = reader.fetch(site, job["base"] + "/")
        declared: list[str] = []
        for policy in reader.policies.values():
            if isinstance(policy, RobotsPolicy):
                declared.extend(policy.sitemaps)
        if home.ok:
            # Uma resposta binária não comprova declaração de feed em HTML.
            with suppress(UnicodeError):
                declared += list(find_feed_links(home.content, home.final_url or home.url))
            try:
                tree: Any = html.fromstring(home.content)
                declared += [
                    u
                    for u in tree.xpath("//a/@href")
                    if any(t in u.lower() for t in ("sitemap", "/rss", "/feed"))
                ]
            except (ValueError, TypeError, etree.ParserError):
                pass
        from urllib.parse import urljoin

        declared = [urljoin(home.final_url or job["base"], u) for u in declared]
        pending = [
            (u, 0, "declaração do site") for u in dict.fromkeys(declared) if reader.allowed(site, u)
        ][:4]
        pending += [(u, 0, "inventário ou padrão conhecido") for u in job["seeds"]]
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        article_seen: set[str] = set()
        while pending and len(records) < 12 and time.monotonic() - reader.started < 180:
            url, depth, origin = pending.pop(0)
            if url in seen or not reader.allowed(site, url):
                continue
            seen.add(url)
            result = reader.fetch(site, url)
            record: dict[str, Any] = dict(
                fonte=site.key,
                url=url,
                observado_em=datetime.now(UTC).isoformat(),
                origem=origin,
                profundidade=depth,
                xml_valido=False,
                tipo="",
                http_status=result.status,
                detalhe=result.error or "",
                final_url=result.final_url,
                candidatas=0,
                filhos_listados=0,
                amostras=[],
                podcast_xml=False,
            )
            if result.ok:
                try:
                    xml = review_xml(result.content, result.final_url or url)
                except (ValueError, OverflowError) as error:
                    record["detalhe"] = str(error)[:300]
                else:
                    record.update(
                        xml_valido=True,
                        tipo=xml.kind,
                        candidatas=len(xml.entries),
                        filhos_listados=len(xml.children),
                        podcast_xml=xml.podcast,
                    )
                    # Reserva leitura de filhos logo após o índice; nunca expande sem limite.
                    if depth < 2 and xml.children:
                        indexes = sorted({0, len(xml.children) // 2, len(xml.children) - 1})
                        pending = [(xml.children[i], depth + 1, url) for i in indexes] + pending
                    for article in xml.entries:
                        if len(article_seen) >= 4:
                            break
                        if (
                            article in article_seen
                            or not site.owns(article)
                            or not reader.allowed(site, article)
                        ):
                            continue
                        article_seen.add(article)
                        fetched = reader.fetch(site, article)
                        sample = dict(
                            url=article,
                            http_status=fetched.status,
                            erro=fetched.error or "",
                            texto_util_confirmado=False,
                            audio_dedicado=False,
                        )
                        if fetched.ok:
                            now = datetime.now(UTC)
                            sample.update(
                                audit_page(
                                    PageSnapshot(
                                        article,
                                        fetched.status,
                                        fetched.content_type,
                                        fetched.final_url,
                                        fetched.content,
                                    ),
                                    since=now - timedelta(days=2),
                                    until=now,
                                    now=now,
                                )
                            )
                        record["amostras"].append(sample)
                        # Um texto já confirma esta rota; reserva amostras para outro endpoint.
                        if sample["texto_util_confirmado"]:
                            break
            records.append(record)
            samples.save(OUT / "resultados" / (samples.identity(site.key, url) + ".json"), record)
        good = [r for r in records if r["xml_valido"]]
        texts = [s for r in records for s in r["amostras"] if s["texto_util_confirmado"]]
        summary = dict(
            key=site.key,
            name=site.name,
            before=job["before"]["functional"],
            xml_tested=len(records),
            functional=len(good),
            texts=len(texts),
            requests=reader.requests,
            cache_hits=reader.cache_hits,
            home_status=home.status,
            home_error=home.error,
            official_declarations=declared,
            records=records,
            completed_at=datetime.now(UTC).isoformat(),
            remaining_not_executed=len(pending),
        )
        samples.save(target, summary)
        print(
            json.dumps(
                {k: v for k, v in summary.items() if k not in ("records", "official_declarations")},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return summary


def safe_audit(job: dict[str, Any], site: Any) -> dict[str, Any]:
    """Registra falha isolada sem abortar as demais fontes do plano congelado."""
    try:
        return audit(job, site)
    except Exception as error:
        result = dict(
            key=site.key,
            name=site.name,
            before=job["before"]["functional"],
            xml_tested=0,
            functional=0,
            texts=0,
            requests=0,
            cache_hits=0,
            home_status=None,
            home_error="erro_interno: " + type(error).__name__ + ": " + str(error)[:200],
            official_declarations=[],
            records=[],
            completed_at=datetime.now(UTC).isoformat(),
            remaining_not_executed=len(job["seeds"]),
            incomplete=True,
        )
        samples.save(OUT / "fontes" / (site.key.replace("/", "__") + ".json"), result)
        return result


def main() -> None:
    """Executa exatamente o plano fixo; saídas independentes da rodada 12."""
    global OUT, KEYS, ALL_PROBLEMS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, choices=(1, 2), default=1)
    parser.add_argument("--all-problems", action="store_true")
    args = parser.parse_args()
    if args.batch == 2:
        OUT = reports.ROOT / "var/auditoria-dirigida-10-02"
        KEYS = BATCH_TWO
    if args.all_problems:
        ALL_PROBLEMS = True
        OUT = reports.ROOT / "var/auditoria-final-problemas"
    OUT.mkdir(parents=True, exist_ok=True)
    if ALL_PROBLEMS:
        backup = OUT / "painel-anterior"
        backup.mkdir(exist_ok=True)
        for file in (reports.ROOT / "var/consulta-funcionais").iterdir():
            if file.is_file() and not (backup / file.name).exists():
                shutil.copy2(file, backup / file.name)
    for name in ("fontes", "resultados"):
        (OUT / name).mkdir(exist_ok=True)
    samples.OUTPUT = OUT
    plan = prepare()
    sites = {
        s.key: s
        for s in load_sites(reports.ROOT / "var/validacao-final/catalogo-congelado.csv").sites
    }
    samples.save(
        OUT / "execucao.json", dict(status="running", started_at=datetime.now(UTC).isoformat())
    )
    results = []
    with ThreadPoolExecutor(max_workers=plan["workers"]) as pool:
        for group in (1, 2, 3):
            futures = [
                pool.submit(safe_audit, job, sites[job["key"]])
                for job in plan["jobs"]
                if job.get("group", 1) == group
            ]
            for future in as_completed(futures):
                results.append(future.result())
                samples.save(
                    OUT / "execucao.json",
                    dict(
                        status="running",
                        group=group,
                        total=len(plan["jobs"]),
                        completed=len(results),
                        recovered=sum(r["before"] == 0 and r["functional"] > 0 for r in results),
                        updated_at=datetime.now(UTC).isoformat(),
                    ),
                )
    results.sort(key=lambda r: r["key"])
    summary = dict(
        status="completed",
        finished_at=datetime.now(UTC).isoformat(),
        sources=len(results),
        sources_recovered=sum(r["before"] == 0 and r["functional"] > 0 for r in results),
        sources_with_text=sum(r["texts"] > 0 for r in results),
        mechanisms_tested=sum(r["xml_tested"] for r in results),
        mechanisms_functional=sum(r["functional"] for r in results),
        requests=sum(r["requests"] for r in results),
    )
    samples.save(OUT / "execucao.json", summary)
    reports.OUTPUT = OUT
    source_fields = (
        "key",
        "name",
        "before",
        "xml_tested",
        "functional",
        "texts",
        "requests",
        "cache_hits",
        "home_status",
        "home_error",
        "completed_at",
        "remaining_not_executed",
        "incomplete",
    )
    export_rows = [
        {field: row.get(field, False if field == "incomplete" else "") for field in source_fields}
        for row in results
    ]
    reports.write_csv("fontes.csv", export_rows)
    reports.workbook(
        {
            "Fontes": [
                {k: v for k, v in r.items() if k not in ("records", "official_declarations")}
                for r in results
            ],
            "Mecanismos": [
                {k: v for k, v in r.items() if k != "amostras"}
                for source in results
                for r in source["records"]
            ],
        }
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
