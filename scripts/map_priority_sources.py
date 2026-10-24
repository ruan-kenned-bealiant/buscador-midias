"""Rodada finita de descoberta e prova textual dos 60 veículos selecionados.

Não usa APIs de IA. Artefatos e checkpoints separados; aplicação via catálogo auditado.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from buscador_midias.catalog.bright_reader import BrightTestReader
from buscador_midias.catalog.health_policy import classify
from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.catalog.reader import TestLimitError
from buscador_midias.catalog.store import CatalogError, clean_url
from buscador_midias.catalog.testing import run_test
from buscador_midias.discovery.feeds import find_feed_links
from buscador_midias.domain.site import Site

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/gerenciador/mapeamento-prioritario"
PRINT_LOCK = threading.Lock()
# Domínios externos oficiais de distribuição; não inclui agregadores de terceiros.
DISTRIBUTION = {
    429: [
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.bbci.co.uk/news/technology/rss.xml",
    ],
    93: ["https://feeds.bbci.co.uk/portuguese/rss.xml"],
    499: [
        "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada",
        "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/internacional/portada",
    ],
    375: [
        "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
        "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
        "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
        "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    ],
}
PAGES = {
    442: [
        "https://www.lemonde.fr/le-monde-et-vous/article/2025/07/14/les-flux-rss-du-monde-fr_5498778_3237.html",
        "https://www.lemonde.fr/en/about-us/article/2026/03/27/le-monde-rss-feeds_6751860_115.html",
    ],
    82: [
        "https://www.tecmundo.com.br/institucional/2382-rss-baixaki.htm",
        "https://www.tecmundo.com.br/rss/62-todas-as-noticias-em-um-unico-lugar.htm",
    ],
    429: ["https://support.bbc.co.uk/platform/feeds/NewsFeeds.htm"],
}
SEEDS = {
    22: [
        "https://www.band.com.br/sitemap.xml",
        "https://www.band.com.br/sitemap_index.xml",
        "https://www.band.com.br/noticias/rss.xml",
    ],
    376: [
        "https://apnews.com/index.rss",
        "https://apnews.com/news-sitemap.xml",
        "https://apnews.com/sitemap.xml",
    ],
    19: ["https://g1.globo.com/rss/g1/globonews/"],
    28: ["https://cbn.globo.com/rss/cbn.xml", "https://cbn.globo.com/sitemap.xml"],
    158: ["https://extra.globo.com/rss.xml", "https://extra.globo.com/sitemap/extra/sitemap.xml"],
    85: ["https://ge.globo.com/rss/ge/", "https://ge.globo.com/sitemap/ge/sitemap.xml"],
    51: ["https://www.nexojornal.com.br/rss.xml"],
    63: ["https://apublica.org/feed/"],
    55: ["https://www.intercept.com.br/feed/"],
    82: ["https://rss.tecmundo.com.br/feed", "https://www.tecmundo.com.br/rss.xml"],
    442: [
        "https://www.lemonde.fr/rss/une.xml",
        "https://www.lemonde.fr/rss/en_continu.xml",
        "https://www.lemonde.fr/international/rss_full.xml",
        "https://www.lemonde.fr/politique/rss_full.xml",
        "https://www.lemonde.fr/economie/rss_full.xml",
        "https://www.lemonde.fr/en/international/rss_full.xml",
    ],
    509: [
        "https://www.repubblica.it/rss/homepage/rss2.0.xml",
        "https://www.repubblica.it/rss/politica/rss2.0.xml",
        "https://www.repubblica.it/rss/esteri/rss2.0.xml",
        "https://www.repubblica.it/rss/economia/rss2.0.xml",
    ],
    432: ["https://www.thetimes.com/sitemap.xml", "https://www.thetimes.co.uk/sitemap.xml"],
    740: [
        "https://www.scmp.com/rss/91/feed",
        "https://www.scmp.com/rss/2/feed",
        "https://www.scmp.com/rss/3/feed",
        "https://www.scmp.com/rss/4/feed",
    ],
    1272: [
        "https://www3.nhk.or.jp/rss/news/cat0.xml",
        "https://www3.nhk.or.jp/nhkworld/en/news/feeds/",
        "https://www3.nhk.or.jp/nhkworld/en/news/rss.xml",
    ],
    702: ["https://www.japantimes.co.jp/feed/", "https://www.japantimes.co.jp/news/feed/"],
    726: [
        "https://english.news.cn/rss/worldrss.xml",
        "https://www.news.cn/english/rss/worldrss.xml",
    ],
    729: [
        "https://www.chinadaily.com.cn/rss/china_rss.xml",
        "https://www.chinadaily.com.cn/rss/world_rss.xml",
    ],
    784: ["https://news.abs-cbn.com/rss", "https://www.abs-cbn.com/sitemap.xml"],
    394: [
        "https://www.politico.com/rss/politicopicks.xml",
        "https://www.politico.com/rss/congress.xml",
        "https://www.politico.com/rss/politics08.xml",
    ],
    1265: ["https://efe.com/feed/", "https://efe.com/mundo/feed/"],
}


def stamp():
    """Data UTC para os checkpoints."""
    return datetime.now(UTC).isoformat()


def write(path, data):
    """Grava JSON atomicamente."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def log(*parts):
    """Emite progresso sem misturar as threads."""
    with PRINT_LOCK:
        print(stamp(), *parts, flush=True)


def site_of(source):
    """Cria o escopo editorial da fonte."""
    return Site.create(
        name=source["name"],
        url=source["url"],
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )


def editorial(url):
    """Evita gastar a amostra em arquivos mensais/diários e conteúdo só multimídia."""
    p = urlsplit(url).path.lower()
    return not (
        re.search(r"(?:19|20)\d{2}[/_-](?:0[1-9]|1[0-2])", p)
        or any(
            x in p
            for x in [
                "podcast",
                "video",
                "audio",
                "image",
                "author",
                "tag-sitemap",
                "category-sitemap",
                "comments",
            ]
        )
    )


def discover(source, repository):
    """Coleta uma lista finita de pistas oficiais e hipóteses."""
    path = OUT / "descoberta" / f"{source['id']}.json"
    if path.exists():
        return json.loads(path.read_text())
    site = site_of(source)
    base = source["url"].rstrip("/")
    origin = urlsplit(base)
    origin = f"{origin.scheme}://{origin.netloc}"
    data = dict(source=source, started_at=stamp(), candidates=[], pages=[], warnings=[])
    known = set()

    def add(url, provenance, priority=5):
        try:
            url = clean_url(url)
        except CatalogError:
            return
        if url in known or not editorial(url):
            return
        if (
            site.path_prefix
            and provenance == "robots.txt oficial"
            and not urlsplit(url).path.startswith(site.path_prefix + "/")
        ):
            return
        allowed = (
            site.owns_host(url)
            or url in DISTRIBUTION.get(source["id"], [])
            or url in SEEDS.get(source["id"], [])
        )
        if not allowed:
            return
        known.add(url)
        data["candidates"].append(dict(url=url, provenance=provenance, priority=priority))

    for url in DISTRIBUTION.get(source["id"], []) + SEEDS.get(source["id"], []):
        add(url, "candidato dirigido; validação obrigatória", -1)
    # Pistas da pesquisa web, sempre restritas ao domínio oficial, não agregadores.
    research = OUT / "pesquisas-web.json"
    page_hints = []
    if research.exists():
        for batch in json.loads(research.read_text()):
            for url in re.findall(r'https?://[^\s<>"`)\]}]+', batch["text"]):
                url = url.rstrip(".,;'")
                if site.owns_host(url) and any(
                    x in url.lower() for x in ["rss", "feed", "sitemap"]
                ):
                    if re.search(r"\.xml(?:\?|$)|/feed/?$|/rss/?$", url):
                        add(url, "pesquisa web: " + batch["batch"], 1)
                    else:
                        page_hints.append(url)
    reader = BrightTestReader(
        [
            base,
            *DISTRIBUTION.get(source["id"], []),
            *SEEDS.get(source["id"], []),
            *PAGES.get(source["id"], []),
        ],
        lambda _: None,
        seconds=60,
        requests=24,
        max_bright_requests=4,
    )
    try:
        # Ler a declaração de política não representa ignorá-la: todas as páginas abaixo usam get().
        response = reader._fetch(origin + "/robots.txt", robots=True)
        data["pages"].append(
            dict(url=origin + "/robots.txt", status=response.status, error=response.error)
        )
        if response.ok:
            for url in re.findall(
                r"^\s*sitemap\s*:\s*(\S+)", response.content.decode("utf-8", "replace"), re.I | re.M
            ):
                add(url, "robots.txt oficial", 0)
        pages = list(
            dict.fromkeys(
                [base, *PAGES.get(source["id"], []), *page_hints, base + "/rss", base + "/feeds"]
            )
        )[:5]
        for url in pages:
            response = reader.get(url)
            data["pages"].append(dict(url=url, status=response.status, error=response.error))
            if not response.ok:
                continue
            for feed in find_feed_links(response.content, response.final_url or url):
                add(feed, "autodiscovery HTML oficial", 0)
            try:
                doc = html.fromstring(response.content, base_url=response.final_url or url)
                for href in doc.xpath("//a/@href"):
                    absolute = urljoin(response.final_url or url, href)
                    if any(x in absolute.lower() for x in ("rss", "feed", "sitemap")):
                        add(absolute, "link em página oficial: " + url, 2)
            except (ValueError, TypeError, etree.ParserError):
                pass
    except (TestLimitError, CatalogError, OSError, ValueError) as error:
        data["warnings"].append(str(error))
    # Endereços convencionais são hipóteses, nunca evidência de funcionamento.
    for suffix in [
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/sitemap-news.xml",
        "/news-sitemap.xml",
        "/feed/",
        "/rss.xml",
        "/rss",
        "/?feed=rss2",
    ]:
        add(base + suffix, "hipótese convencional", 8)
    with repository.connection() as db:
        existing = db.execute(
            "SELECT url,active,deleted_at,last_test FROM links WHERE source_id=%s ORDER BY id",
            (source["id"],),
        ).fetchall()
    for row in existing:
        if row["deleted_at"] is None and row["active"]:
            add(row["url"], "índice existente: procurar filhos de editorias", 3)
        elif classify(row["last_test"] or {}) in ("invalid", "inconclusive", "no_news"):
            add(row["url"], "endereço histórico para reavaliar", 6)
    data.update(
        finished_at=stamp(), requests=reader.requests, bright_data_requests=reader.bright_requests
    )
    data["candidates"] = sorted(data["candidates"], key=lambda r: r["priority"])[:40]
    write(path, data)
    log("DESCOBERTA", source["name"], len(data["candidates"]), "candidatos")
    return data


def test_source(discovery, repository):
    """Valida XML e notícia textual com orçamento por fonte."""
    source = discovery["source"]
    ident = source["id"]
    path = OUT / "testes" / f"{ident}.json"
    if path.exists() and json.loads(path.read_text()).get("completed"):
        return
    data = (
        json.loads(path.read_text())
        if path.exists()
        else dict(source=source, started_at=stamp(), results=[], preflight=[], warnings=[])
    )
    source = repository.store.source(ident)
    data["source"] = source
    if discovery.get("source_url_override"):
        data["proposed_source_url"] = discovery["source_url_override"]
        source = dict(source, url=discovery["source_url_override"])
    site = site_of(source)
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
    seen = {r["url"] for r in data["preflight"]}
    seen_tests = {r["url"] for r in data["results"]}
    # Um leitor compartilhado limita a sondagem de XML e evita repetir robots em cada endereço.
    reader = BrightTestReader(
        [source["url"], *(c["url"] for c in candidates)],
        lambda _: None,
        seconds=180,
        requests=48,
        max_bright_requests=6,
    )
    started = time.monotonic()
    validated = 0
    try:
        for position, candidate in enumerate(candidates):
            if len(data["preflight"]) >= 32:
                break
            url = candidate["url"]
            if url in seen:
                continue
            if time.monotonic() - started > 600:
                break
            row = dict(candidate)
            data["preflight"].append(row)
            seen.add(url)
            try:
                response = reader.get(url)
                row.update(status=response.status, error=response.error)
                if not response.ok:
                    continue
                doc = parse_document(response.content, response.final_url or url, 16 * 1024 * 1024)
                row.update(
                    kind=doc["kind"],
                    entries=len(doc["entries"]),
                    children=len(doc["children"]),
                    index=doc["index"],
                )
            except (ValueError, TestLimitError, CatalogError, OSError) as error:
                row["error"] = str(error)
                if isinstance(error, TestLimitError):
                    break
                continue
            # Índices existentes ajudam a localizar novos caminhos de editorias, sem duplicar o pai.
            children = [c for c in doc["children"] if editorial(c) and site.owns_host(c)]
            # Não expandir a fila indefinidamente: até 6 filhos por índice e 32 sondagens por fonte.
            for child in children[:6]:
                if child not in {c["url"] for c in candidates} and len(candidates) < 48:
                    candidates.insert(
                        position + 1,
                        dict(url=child, provenance="filho declarado: " + url, priority=2),
                    )
            if url in proven or url in seen_tests:
                continue
            if not doc["entries"] and not doc["children"]:
                continue
            model = (
                "rss"
                if doc["kind"] == "feed"
                else "sitemap_tree"
                if doc["index"]
                else "news_sitemap"
                if doc["news"]
                else "sitemap"
            )
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
                bright_data=True,
                max_bright_requests=8,
            )
            test_started = time.monotonic()
            result = run_test(config, source, lambda _: None)
            reader.deadline += time.monotonic() - test_started
            data["results"].append(
                dict(
                    url=url,
                    provenance=candidate["provenance"],
                    category=classify(result),
                    result=result,
                )
            )
            seen_tests.add(url)
            validated += 1
            write(path, data)
            log("TESTE", source["name"], url, classify(result))
            if validated >= 12:
                break
    except (TestLimitError, CatalogError, OSError, ValueError) as error:
        data["warnings"].append(str(error))
    finally:
        data.update(
            completed=True,
            finished_at=stamp(),
            preflight_requests=reader.requests,
            preflight_bright_data_requests=reader.bright_requests,
        )
        write(path, data)
        log("FONTE CONCLUÍDA", source["name"], len(data["results"]), "testes")


def main():
    """Executa uma etapa retomável dos alvos congelados."""
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["discover", "test"])
    args = parser.parse_args()
    repository = HealthStore(PostgresCatalogStore())
    repository.initialize()
    targets = json.loads((OUT / "alvos.json").read_text())["alvos"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        if args.stage == "discover":
            tasks = [pool.submit(discover, s, repository) for s in targets]
        else:
            tasks = [
                pool.submit(
                    test_source,
                    json.loads((OUT / "descoberta" / f"{s['id']}.json").read_text()),
                    repository,
                )
                for s in targets
            ]
        for task in as_completed(tasks):
            task.result()
    log("ETAPA ENCERRADA", args.stage, len(targets), "fontes")


if __name__ == "__main__":
    main()
