"""Rodada finita de mapeamento dos principais sites, com Bright Data limitado por site.

Descobre feeds e sitemaps (robots.txt, HTML da capa, links, endereços convencionais), valida cada
candidato com notícia e texto reais e grava propostas. Não altera o catálogo: a aplicação é
separada. O fallback pago tem teto de ``BRIGHT_PER_SITE`` chamadas por site, somado entre a
descoberta e o teste, e só entra depois de duas tentativas diretas.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from buscador_midias.catalog.editorial_mapping import purpose
from buscador_midias.catalog.health_policy import classify
from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.http_transport import make_reader
from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.catalog.reader import TestLimitError
from buscador_midias.catalog.site_budget import BRIGHT_PER_SITE, SiteBudget, budgeted_reader
from buscador_midias.catalog.store import CatalogError, clean_url
from buscador_midias.catalog.testing import run_test
from buscador_midias.discovery.feeds import find_feed_links
from map_priority_sources import DISTRIBUTION, PAGES, SEEDS, editorial, log, site_of, stamp, write

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ROOT / "var/maintenance/mapeamento/principais-candidatos.json"
OUT = ROOT / "var/maintenance/mapeamento/rodada-principais"
CONVENTIONAL = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-news.xml",
    "/news-sitemap.xml",
    "/feed/",
    "/rss.xml",
    "/rss",
    "/?feed=rss2",
)


class Layered:
    """Tenta primeiro o transporte que já funciona no site; só então o leitor com fallback pago."""

    def __init__(self, primary, paid) -> None:
        """Guarda o leitor do transporte conhecido (ou ``None``) e o leitor com orçamento."""
        self.primary, self.paid = primary, paid

    @property
    def deadline(self):
        """Prazo do leitor pago; o do transporte conhecido é ajustado junto."""
        return self.paid.deadline

    @deadline.setter
    def deadline(self, value):
        self.paid.deadline = value
        if self.primary is not None:
            self.primary.deadline = value

    def get(self, url):
        """Lê pelo transporte conhecido e, se falhar, deixa o leitor pago decidir."""
        if self.primary is not None:
            response = self.primary.get(url)
            if response.ok:
                return response
        return self.paid.get(url)

    def _fetch(self, url, *, robots=False):
        return self.paid._fetch(url, robots=robots)


def dominant_transport(repository, ident):
    """Transporte que a maioria dos links do site já usa com sucesso; ``direct`` se não há dado."""
    with repository.connection() as db:
        rows = db.execute(
            "SELECT access_transport t,COUNT(*) n FROM links WHERE source_id=%s "
            "AND deleted_at IS NULL GROUP BY 1 ORDER BY 2 DESC",
            (ident,),
        ).fetchall()
    return rows[0]["t"] if rows and rows[0]["t"] != "direct" else "direct"


def reader_for(budget, transport, roots, **kwargs):
    """Leitor da descoberta: transporte conhecido primeiro, Bright Data com orçamento depois."""
    paid = budgeted_reader(budget, roots, lambda _: None, **kwargs)
    if transport == "direct":
        return paid
    primary = make_reader(roots, lambda _: None, transport=transport, **kwargs)
    return Layered(primary, paid)


def discover(source, repository):
    """Coleta uma lista finita de candidatos com proveniência."""
    path = OUT / "descoberta" / f"{source['id']}.json"
    if path.exists():
        return json.loads(path.read_text())
    site = site_of(source)
    base = source["url"].rstrip("/")
    parts = urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    budget = SiteBudget(BRIGHT_PER_SITE)
    transport = dominant_transport(repository, source["id"])
    data = dict(
        source=source, started_at=stamp(), transport=transport, candidates=[], pages=[], warnings=[]
    )
    known: set[str] = set()

    def add(url, provenance, priority=5):
        try:
            url = clean_url(url)
        except CatalogError:
            return
        if url in known or not editorial(url):
            return
        hinted = url in DISTRIBUTION.get(source["id"], []) or url in SEEDS.get(source["id"], [])
        if not (site.owns_host(url) or hinted):
            return
        known.add(url)
        data["candidates"].append(dict(url=url, provenance=provenance, priority=priority))

    for url in DISTRIBUTION.get(source["id"], []) + SEEDS.get(source["id"], []):
        add(url, "candidato dirigido; validação obrigatória", -1)
    roots = [base, *DISTRIBUTION.get(source["id"], []), *SEEDS.get(source["id"], [])]
    reader = reader_for(budget, transport, roots, seconds=90, requests=24)
    try:
        response = reader._fetch(origin + "/robots.txt", robots=True)
        data["pages"].append(
            dict(url=origin + "/robots.txt", status=response.status, error=response.error)
        )
        if response.ok:
            body = response.content.decode("utf-8", "replace")
            for url in re.findall(r"^\s*sitemap\s*:\s*(\S+)", body, re.I | re.M):
                add(url, "robots.txt oficial", 0)
        pages = list(dict.fromkeys([base, *PAGES.get(source["id"], []), base + "/rss"]))[:4]
        for url in pages:
            response = reader.get(url)
            data["pages"].append(dict(url=url, status=response.status, error=response.error))
            if not response.ok:
                continue
            final = response.final_url or url
            for feed in find_feed_links(response.content, final):
                add(feed, "autodescoberta HTML oficial", 0)
            try:
                document = html.fromstring(response.content, base_url=final)
                for href in document.xpath("//a/@href"):
                    absolute = urljoin(final, href)
                    if any(x in absolute.lower() for x in ("rss", "feed", "sitemap")):
                        add(absolute, "link em página oficial: " + url, 2)
            except (ValueError, TypeError, etree.ParserError):
                pass
    except (TestLimitError, CatalogError, OSError, ValueError) as error:
        data["warnings"].append(str(error))
    for suffix in CONVENTIONAL:
        add(base + suffix, "hipótese convencional", 8)
    with repository.connection() as db:
        existing = db.execute(
            "SELECT url,active,deleted_at,last_test FROM links WHERE source_id=%s ORDER BY id",
            (source["id"],),
        ).fetchall()
    for row in existing:
        if row["deleted_at"] is None and row["active"]:
            add(row["url"], "índice existente: procurar filhos de editorias", 3)
    data.update(finished_at=stamp(), bright_used=budget.spent)
    data["candidates"] = sorted(data["candidates"], key=lambda r: r["priority"])[:40]
    write(path, data)
    log("DESCOBERTA", source["name"], len(data["candidates"]), "candidatos", budget.spent, "pagas")
    return data


def paid_broken():
    """O provedor pago recusa as credenciais (407) em vários sites: não adianta insistir."""
    refused = 0
    for file in (OUT / "descoberta").glob("*.json"):
        pages = json.loads(file.read_text())["pages"]
        refused += any("proxy HTTP 407" in (p["error"] or "") for p in pages)
    return refused >= 10


def model_of(doc):
    """Mecanismo do catálogo que corresponde ao documento lido."""
    if doc["kind"] == "feed":
        return "rss"
    return "sitemap_tree" if doc["index"] else "news_sitemap" if doc["news"] else "sitemap"


def test_source(discovery, repository):
    """Valida XML e notícia textual, com o que resta do orçamento pago do site."""
    source = discovery["source"]
    ident = source["id"]
    path = OUT / "testes" / f"{ident}.json"
    if path.exists() and json.loads(path.read_text()).get("completed"):
        return
    budget = SiteBudget(BRIGHT_PER_SITE)
    budget.spend(discovery.get("bright_used", 0))
    if paid_broken():
        budget.spend(BRIGHT_PER_SITE)
    transport = discovery.get("transport", "direct")
    site = site_of(source)
    data = dict(source=source, started_at=stamp(), transport=transport, results=[], preflight=[])
    data["warnings"] = []
    with repository.connection() as db:
        existing = db.execute(
            "SELECT url,active,deleted_at,last_test FROM links WHERE source_id=%s", (ident,)
        ).fetchall()
    proven = {
        r["url"]
        for r in existing
        if r["deleted_at"] is None and r["active"] and (r["last_test"] or {}).get("usable_for_news")
    }
    candidates = list(discovery["candidates"])
    seen: set[str] = set()
    roots = [source["url"], *(c["url"] for c in candidates)]
    reader = reader_for(budget, transport, roots, seconds=180, requests=48)
    started = time.monotonic()
    validated = 0
    try:
        for position, candidate in enumerate(candidates):
            url = candidate["url"]
            if len(data["preflight"]) >= 32 or time.monotonic() - started > 600:
                break
            if url in seen:
                continue
            seen.add(url)
            row = dict(candidate)
            data["preflight"].append(row)
            try:
                response = reader.get(url)
                row.update(status=response.status, error=response.error)
                if not response.ok:
                    continue
                doc = parse_document(response.content, response.final_url or url, 16 * 1024 * 1024)
                row.update(kind=doc["kind"], entries=len(doc["entries"]), index=doc["index"])
            except (ValueError, TestLimitError, CatalogError, OSError) as error:
                row["error"] = str(error)
                if isinstance(error, TestLimitError):
                    break
                continue
            for child in [c for c in doc["children"] if editorial(c) and site.owns_host(c)][:6]:
                if child not in {c["url"] for c in candidates} and len(candidates) < 48:
                    candidates.insert(
                        position + 1,
                        dict(url=child, provenance="filho declarado: " + url, priority=2),
                    )
            if url in proven or (not doc["entries"] and not doc["children"]):
                continue
            model = model_of(doc)
            settings = repository.store.model_settings(model)
            parameters = dict(
                settings["parameters"],
                lookback_days=0,
                timeout_seconds=60,
                article_samples=3,
                sample_order="distributed",
            )
            config = dict(
                test_id=uuid.uuid4().hex,
                url=url,
                kind=doc["kind"],
                model=model,
                model_version=settings["version"],
                parameters=parameters,
                access_transport=transport,
            )
            factory = None
            if transport == "direct":

                def factory(roots, progress, parameters=parameters):
                    return budgeted_reader(
                        budget,
                        roots,
                        progress,
                        seconds=parameters["timeout_seconds"],
                        requests=parameters["max_requests"],
                        max_bytes=parameters["max_response_mib"] * 1024 * 1024,
                    )

            test_started = time.monotonic()
            result = run_test(config, source, lambda _: None, factory)
            reader.deadline += time.monotonic() - test_started
            data["results"].append(
                dict(
                    url=url,
                    provenance=candidate["provenance"],
                    category=classify(result),
                    model=model,
                    result=result,
                )
            )
            validated += 1
            write(path, data)
            if validated >= 12:
                break
    except (TestLimitError, CatalogError, OSError, ValueError) as error:
        data["warnings"].append(str(error))
    finally:
        data.update(completed=True, finished_at=stamp(), bright_used=budget.spent)
        write(path, data)
        log(
            "FONTE CONCLUÍDA", source["name"], len(data["results"]), "testes", budget.spent, "pagas"
        )


def report(repository):
    """Resume as propostas: links comprovados que ainda não estão ativos no catálogo."""
    proposals, bright, failed, tested = [], 0, [], 0
    for file in sorted((OUT / "testes").glob("*.json")):
        data = json.loads(file.read_text())
        source = data["source"]
        found = OUT / "descoberta" / f"{source['id']}.json"
        # As chamadas pagas reais são as da descoberta; no teste o gasto só bloqueia o provedor.
        bright += (
            json.loads(found.read_text()).get("bright_used", 0)
            if paid_broken()
            else (data.get("bright_used", 0))
        )
        with repository.connection() as db:
            active = {
                r["url"]
                for r in db.execute(
                    "SELECT url FROM links WHERE source_id=%s AND active=1 AND deleted_at IS NULL",
                    (source["id"],),
                ).fetchall()
            }
        # Fatia por data (?yyyy=…&dd=…) ou arquivo mensal não é rota durável de atualização.
        durable = [r for r in data["results"] if purpose(r["result"]) == "updates"]
        good = [r for r in durable if r["category"] == "healthy" and r["url"] not in active]
        tested += len(data["results"])
        if not active and not good:
            failed.append(source["name"])
        proposals += [
            dict(site_id=source["id"], site=source["name"], url=r["url"], model=r["model"])
            for r in good
        ]
    summary = dict(
        sites=len(list((OUT / "testes").glob("*.json"))),
        tested=tested,
        new_links=len(proposals),
        sites_with_new=len({p["site_id"] for p in proposals}),
        bright_calls=bright,
        bright_credentials_refused=paid_broken(),
        sites_without_working_link=failed,
        by_model=dict(Counter(p["model"] for p in proposals)),
    )
    write(OUT / "propostas.json", dict(summary=summary, proposals=proposals))
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def main():
    """Executa uma etapa retomável sobre os alvos congelados."""
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["discover", "test", "report"])
    parser.add_argument(
        "--only", type=int, nargs="+", help="Ids de sites (padrão: todos os alvos)."
    )
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    repository = HealthStore(PostgresCatalogStore())
    repository.initialize()
    if args.stage == "report":
        return report(repository)
    alvos = json.loads(TARGETS.read_text())["alvos"]
    ids = args.only or sorted(
        {*alvos["ativos"], *alvos["inativos_sem_link_funcional"], *alvos["novos"]}
    )
    sources = [repository.store.source(i) for i in ids]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        if args.stage == "discover":
            tasks = [pool.submit(discover, s, repository) for s in sources]
        else:
            tasks = [
                pool.submit(
                    test_source,
                    json.loads((OUT / "descoberta" / f"{s['id']}.json").read_text()),
                    repository,
                )
                for s in sources
            ]
        for task in as_completed(tasks):
            task.result()
    log("ETAPA ENCERRADA", args.stage, len(sources), "fontes")


if __name__ == "__main__":
    main()
