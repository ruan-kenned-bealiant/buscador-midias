"""Auditoria finita, HTTP e sem IA, de veículos editoriais prioritários.

Não inicia ingestão nem modifica o catálogo. Provas completas e revisões congeladas
permitem aplicar os resultados positivos separadamente, após revisão editorial.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from lxml import html
from psycopg.rows import dict_row

from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.postgres import connect_postgres
from buscador_midias.catalog.reader import TestLimitError, TestReader
from buscador_midias.catalog.testing import run_test
from buscador_midias.discovery.feeds import find_feed_links
from buscador_midias.domain.site import Site
from map_priority_sources import DISTRIBUTION, PAGES, SEEDS

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/gerenciador/prioritarios-editoriais-20261026"
LOCK = threading.Lock()
HOST_LOCKS: dict[str, threading.Lock] = {}


def stamp():
    """Retorna o horário UTC da prova."""
    return datetime.now(UTC).isoformat()


def write(path, data):
    """Persiste um checkpoint atomicamente."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    temp.replace(path)


def credentials():
    """Carrega apenas as credenciais PostgreSQL, sem exibi-las."""
    values = {}
    for line in (ROOT / "../KyX-NovoDD-backend/.env").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.startswith("DB_MIDIAS_IA_"):
            values[key] = value.strip().strip("\"'")
    values["DB_MIDIAS_IA_HOST"] = "127.0.0.1"
    return values


def freeze():
    """Congela as revisões do catálogo e a contagem inicial do corpus."""
    path = OUT / "manifest.json"
    if path.exists():
        return json.loads(path.read_text())
    old = json.loads((ROOT / "var/gerenciador/mapeamento-prioritario/alvos.json").read_text())
    ids = sorted({s["id"] for s in old["alvos"]} | {4, 17, 27})
    with connect_postgres(credentials()) as db:
        db.row_factory = dict_row
        db.execute("SET TRANSACTION READ ONLY")
        sources = db.execute(
            "SELECT * FROM midias_manager.sites WHERE id=ANY(%s) "
            "AND deleted_at IS NULL ORDER BY id",
            (ids,),
        ).fetchall()
        links = db.execute(
            "SELECT * FROM midias_manager.links WHERE source_id=ANY(%s) ORDER BY id", (ids,)
        ).fetchall()
        models = db.execute("SELECT * FROM midias_manager.mechanisms").fetchall()
        # Atribuição à primeira fonte da ingestão, contando artigos realmente gravados.
        corpus = db.execute(
            "SELECT a.source_id,count(*) total,count(*) FILTER(WHERE c.published_at >= "
            "date_trunc('day',now() AT TIME ZONE 'America/Sao_Paulo') AT TIME ZONE "
            "'America/Sao_Paulo') today,max(c.published_at) latest_published "
            "FROM midias_manager.ingest_articles a JOIN public.midias_ia_articles c "
            "ON c.id=a.article_id WHERE a.source_id=ANY(%s) AND length(coalesce(c.body,''))>0 "
            "GROUP BY a.source_id",
            (ids,),
        ).fetchall()
    manifest = dict(
        started_at=stamp(),
        sources=sources,
        links=links,
        models=models,
        corpus=corpus,
        limits=dict(
            workers=10,
            seconds_per_source=180,
            global_seconds=1500,
            http_requests_per_source=64,
            tests_per_source=6,
            candidate_probes_per_source=24,
            article_samples_per_test=3,
            bright_data=False,
            openrouter=False,
        ),
    )
    write(path, manifest)
    return json.loads(path.read_text())


def editorial(url):
    """Separa caminhos comerciais/multimídia sem apagar seu histórico."""
    path = urlsplit(url).path.lower()
    return not any(
        x in path
        for x in (
            "/plus/",
            "podcast",
            "audio",
            "video",
            "image-sitemap",
            "webstor",
            "tag-sitemap",
            "author-sitemap",
            "comments",
            "/sponsored",
            "/branded",
        )
    )


class AuditReader:
    """Cache por fonte, corpos originais comprimidos e orçamento HTTP compartilhado."""

    def __init__(self, roots, directory, deadline):
        """Define escopo, diretório de provas e orçamento da fonte."""
        self.reader = TestReader(
            roots, lambda _: None, seconds=180, requests=64, max_bytes=8 * 1024 * 1024
        )
        self.reader.deadline = deadline
        self.cache = {}
        self.directory = directory
        self.responses = []

    @property
    def requests(self):
        """Conta as chamadas físicas, incluindo redirecionamentos."""
        return self.reader.requests

    def get(self, url):
        """Lê uma URL uma vez nesta rodada e guarda sua prova original."""
        if time.monotonic() >= self.reader.deadline:
            raise TestLimitError("Orçamento de tempo encerrado.")
        if url in self.cache:
            return self.cache[url]
        response = self.reader.get(url)
        self.cache[url] = response
        digest = hashlib.sha256(response.content).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        # Le quota de réponses et la taille maximale bornent aussi le stockage.
        if response.content:
            (self.directory / (digest + ".gz")).write_bytes(gzip.compress(response.content))
        self.responses.append(
            dict(
                url=url,
                final_url=response.final_url,
                status=response.status,
                error=response.error,
                bytes=len(response.content),
                sha256=digest,
            )
        )
        return response


class TestView:
    """Contagem independente por teste sobre o cache compartilhado da fonte."""

    def __init__(self, reader):
        """Marca o início da contagem física deste teste."""
        self.reader = reader
        self.initial = reader.requests

    @property
    def requests(self):
        """Retorna apenas as requisições físicas deste teste."""
        return self.reader.requests - self.initial

    def get(self, url):
        """Compartilha escopo/cache, com limite adicional por teste."""
        if self.requests >= 16:
            raise TestLimitError("Limite HTTP do teste encerrado.")
        return self.reader.get(url)


def audit_source(source, manifest, global_deadline):
    """Serializa fontes do mesmo host e permite retomada por checkpoint."""
    path = OUT / "testes" / f"{source['id']}.json"
    if path.exists() and json.loads(path.read_text()).get("completed"):
        return json.loads(path.read_text())
    host = urlsplit(source["url"]).hostname or ""
    with LOCK:
        host_lock = HOST_LOCKS.setdefault(host, threading.Lock())
    with host_lock:
        return audit_locked(source, manifest, global_deadline, path)


def audit_locked(source, manifest, global_deadline, path):
    """Descobre pistas e valida amostras dentro de um orçamento finito."""
    start = time.monotonic()
    deadline = min(global_deadline, start + 180)
    links = [link for link in manifest["links"] if link["source_id"] == source["id"]]
    special = DISTRIBUTION.get(source["id"], []) + SEEDS.get(source["id"], [])
    # Endereços editoriais públicos conhecidos, todos sujeitos à prova HTTP atual.
    additional = {
        1: [
            "https://feeds.folha.uol.com.br/emcimadahora/rss091.xml",
            "https://feeds.folha.uol.com.br/poder/rss091.xml",
            "https://feeds.folha.uol.com.br/mundo/rss091.xml",
        ],
        4: ["https://valor.globo.com/rss/", "https://valor.globo.com/sitemap/valor/sitemap.xml"],
        373: [
            "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        ],
        378: [
            "https://feeds.bloomberg.com/markets/news.rss",
            "https://feeds.bloomberg.com/politics/news.rss",
        ],
        430: [
            "https://www.theguardian.com/world/rss",
            "https://www.theguardian.com/international/rss",
        ],
        459: ["https://rss.dw.com/rdf/rss-en-all"],
        1216: ["https://www.aljazeera.com/xml/rss/all.xml"],
        377: [
            "https://www.reuters.com/sitemap/",
            "https://www.reuters.com/arc/outboundfeeds/news-sitemap-index/?outputType=xml",
        ],
    }.get(source["id"], [])
    special += additional
    roots = [
        source["url"],
        *(link["url"] for link in links),
        *special,
        *PAGES.get(source["id"], []),
    ]
    reader = AuditReader(roots, OUT / "corpos" / str(source["id"]), deadline)
    data = dict(
        source=source,
        started_at=stamp(),
        results=[],
        preflight=[],
        pages=[],
        warnings=[],
        omitted_non_editorial=[link["url"] for link in links if not editorial(link["url"])],
    )
    candidates = {}
    site = Site.create(
        name=source["name"],
        url=source["url"],
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )

    def add(url, provenance, priority):
        url = url.strip()
        if not url.startswith(("https://", "http://")) or not editorial(url):
            return
        if (
            not site.owns_host(url)
            and url not in special
            and url not in [link["url"] for link in links]
        ):
            return
        old = candidates.get(url)
        if old is None or priority < old["priority"]:
            candidates[url] = dict(url=url, provenance=provenance, priority=priority)

    for url in special:
        add(url, "candidato editorial dirigido", 0)
    for link in links:
        priority = 1 if link["deleted_at"] is None and link["active"] else 5
        # Folhas datadas não substituem uma rota durável de atualizações.
        if re.search(r"/20\d{2}[/_-]", urlsplit(link["url"]).path):
            priority += 5
        add(link["url"], "cadastro existente", priority)
    try:
        origin = urlsplit(source["url"])
        origin = f"{origin.scheme}://{origin.netloc}"
        pages = [origin + "/robots.txt", source["url"], *PAGES.get(source["id"], [])]
        for url in pages[:4]:
            response = reader.get(url)
            data["pages"].append(dict(url=url, status=response.status, error=response.error))
            if not response.ok:
                continue
            if url.endswith("robots.txt"):
                for target in re.findall(
                    r"^\s*sitemap\s*:\s*(\S+)",
                    response.content.decode("utf-8", "replace"),
                    re.I | re.M,
                ):
                    if not site.path_prefix or urlsplit(target).path.startswith(
                        site.path_prefix + "/"
                    ):
                        add(target, "declaração oficial de sitemap", 2)
                continue
            for target in find_feed_links(response.content, response.final_url or url):
                add(target, "RSS declarado no HTML oficial", 0)
            try:
                doc = html.fromstring(response.content)
                for href in doc.xpath("//a/@href"):
                    target = urljoin(response.final_url or url, href)
                    if re.search(r"rss|feed|sitemap", target, re.I):
                        add(target, "link em página oficial: " + url, 3)
            except (ValueError, TypeError):
                pass
        for suffix in [
            "/news-sitemap.xml",
            "/sitemap-news.xml",
            "/sitemap.xml",
            "/sitemap_index.xml",
            "/feed/",
            "/rss.xml",
        ]:
            add(source["url"].rstrip("/") + suffix, "hipótese convencional", 7)
        queue = sorted(candidates.values(), key=lambda c: (c["priority"], c["url"]))
        tested = set()
        position = 0
        models = {m["model"]: m for m in manifest["models"]}
        while position < len(queue) and len(data["preflight"]) < 24 and len(data["results"]) < 6:
            if time.monotonic() >= deadline:
                raise TestLimitError("Orçamento por fonte/global encerrado.")
            item = queue[position]
            position += 1
            url = item["url"]
            if url in tested:
                continue
            tested.add(url)
            row = dict(item)
            data["preflight"].append(row)
            response = reader.get(url)
            row.update(status=response.status, error=response.error)
            if not response.ok:
                continue
            try:
                doc = parse_document(response.content, response.final_url or url, 8 * 1024 * 1024)
            except ValueError as error:
                row["error"] = str(error)
                continue
            row.update(kind=doc["kind"], entries=len(doc["entries"]), children=len(doc["children"]))
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
            if url == "https://g1.globo.com/sitemap/g1/sitemap.xml":
                model = "g1_daily"
            settings = models[model]
            parameters = dict(
                settings["parameters"],
                lookback_days=3,
                timeout_seconds=40,
                article_samples=3,
                sample_order="first",
                max_requests=16,
            )
            if doc["index"]:
                parameters.update(max_documents=4, max_depth=10)
            config = dict(
                test_id=uuid.uuid4().hex,
                url=url,
                kind=doc["kind"],
                model=model,
                model_version=settings["version"],
                parameters=parameters,
                bright_data=False,
            )
            old_deadline = reader.reader.deadline
            reader.reader.deadline = min(deadline, time.monotonic() + 40)
            view = TestView(reader)
            result = run_test(
                config,
                source,
                lambda _: None,
                factory=lambda _roots, _progress, current=view: current,
            )
            reader.reader.deadline = old_deadline
            data["results"].append(dict(item, result=result))
            # Distingue ausência de publicação recente de falha no link.
            if (
                result["xml_valid"]
                and not result["samples"]
                and doc["entries"]
                and len(data["results"]) < 6
            ):
                fallback = dict(
                    config, test_id=uuid.uuid4().hex, parameters=dict(parameters, lookback_days=0)
                )
                reader.reader.deadline = min(deadline, time.monotonic() + 30)
                view = TestView(reader)
                result = run_test(
                    fallback,
                    source,
                    lambda _: None,
                    factory=lambda _roots, _progress, current=view: current,
                )
                reader.reader.deadline = old_deadline
                data["results"].append(dict(item, historical_probe=True, result=result))
            # Até dois filhos por índice; prova amostral, sem promessa de cobertura total.
            children = sorted(
                doc["children"], key=lambda u: (doc["child_dates"].get(u, ""), u), reverse=True
            )
            for child in children[:2]:
                if editorial(child) and site.owns_host(child) and child not in tested:
                    queue.append(dict(url=child, provenance="filho declarado: " + url, priority=4))
            write(path, data)
    except Exception as error:
        data["warnings"].append(f"{type(error).__name__}: {error}")
    data.update(
        completed=True,
        finished_at=stamp(),
        elapsed_seconds=round(time.monotonic() - start, 2),
        requests=reader.requests,
        responses=reader.responses,
        candidates=list(candidates.values()),
        bounded_sample=True,
    )
    write(path, data)
    with LOCK:
        print(
            stamp(),
            source["name"],
            len(data["preflight"]),
            "sondagens",
            len(data["results"]),
            "testes",
            sum(r["result"].get("text_samples", 0) for r in data["results"]),
            "textos",
            flush=True,
        )
    return data


def main():
    """Executa a rodada sem alterar catálogo, agendamento ou corpus."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-only", action="store_true")
    args = parser.parse_args()
    manifest = freeze()
    if args.freeze_only:
        print(len(manifest["sources"]), "fontes congeladas")
        return
    deadline = time.monotonic() + 1500
    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [
            pool.submit(audit_source, source, manifest, deadline) for source in manifest["sources"]
        ]
        for future in as_completed(futures):
            results.append(future.result())
    write(OUT / "resultado.json", dict(finished_at=stamp(), sources=results))
    print("CONCLUÍDO", len(results), "fontes", flush=True)


if __name__ == "__main__":
    main()
