"""Piloto finito de transporte em 20 URLs públicas, sem alterar catálogo ou ingestão."""

from __future__ import annotations

import gzip
import hashlib
import importlib.metadata
import json
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from buscador_midias.catalog.http_policy import retry_after
from buscador_midias.catalog.http_session import HttpSessions
from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.reader import TestReader, public_ip
from buscador_midias.domain.site import Site
from buscador_midias.extraction.reliability import PageSnapshot
from buscador_midias.extraction.sample_audit import audit_page
from buscador_midias.net.fetcher import FetchResult

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/gerenciador/piloto-http-20-20261026"
PREVIOUS = ROOT / "var/gerenciador/prioritarios-editoriais-20261026"
MAX_BYTES = 8 * 1024 * 1024
PRINT_LOCK = threading.Lock()


def stamp():
    """Horário da medição em UTC."""
    return datetime.now(UTC).isoformat()


def write(path, data):
    """Grava checkpoint atomicamente, sem cookies ou credenciais."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    temp.replace(path)


def manifest():
    """Seleciona URLs com falhas documentadas antes do piloto, sem seleção por resultado."""
    path = OUT / "alvos.json"
    if path.exists():
        return json.loads(path.read_text())
    frozen = json.loads((PREVIOUS / "manifest.json").read_text())
    sources = {source["id"]: source for source in frozen["sources"]}
    targets = []

    def add(ident, url, kind, old_status):
        targets.append(
            dict(
                id=len(targets) + 1,
                source_id=ident,
                source=sources[ident]["name"],
                source_url=sources[ident]["url"],
                url=url,
                kind=kind,
                previous_status=old_status,
            )
        )

    for url in [
        "https://g1.globo.com/sitemap/g1/sitemap.xml",
        "https://g1.globo.com/rss/g1/",
        "https://g1.globo.com/rss/g1/politica/",
        "https://g1.globo.com/rss/g1/ac/acre/",
    ]:
        add(15, url, "xml", 503)
    for url, kind in [
        ("https://www.reuters.com/arc/outboundfeeds/news-sitemap-index/?outputType=xml", "xml"),
        ("https://www.reuters.com/arc/outboundfeeds/sitemap-index/?outputType=xml", "xml"),
        ("https://www.reuters.com/sitemap/", "catalog_page"),
    ]:
        add(377, url, kind, 401)
    historical = json.loads(
        (ROOT / "var/gerenciador/reuters-editorial-20261026/articles.json").read_text()
    )
    add(377, historical[0]["url"], "article", 401)
    for ident in [378, 373]:
        previous = json.loads((PREVIOUS / "testes" / f"{ident}.json").read_text())
        urls = []
        for result in previous["results"]:
            for sample in result["result"]["samples"]:
                url = sample["url"]
                if sample.get("status") == 403 and url not in urls and "/videos/" not in url:
                    urls.append(url)
        for url in urls[:3]:
            add(ident, url, "article", 403)
    for ident in [22, 376]:
        previous = json.loads((PREVIOUS / "testes" / f"{ident}.json").read_text())
        for row in [r for r in previous["preflight"] if r.get("status") == 403][:3]:
            add(ident, row["url"], "xml", 403)
    assert len(targets) == 20 and len({t["url"] for t in targets}) == 20
    data = dict(
        created_at=stamp(),
        targets=targets,
        variants=["http_current", "curl_chrome", "curl_chrome_home", "chromium", "http_recheck"],
        limits=dict(
            workers=3,
            seconds_per_variant=45,
            logical_gets_per_variant=6,
            xml_depth=2,
            article_samples=1,
            global_seconds=1200,
            browser_requests=120,
            browser_wait_ms=4000,
        ),
        dependencies={
            name: importlib.metadata.version(name) for name in ["curl_cffi", "playwright"]
        },
        paid_services=False,
        catalog_mutations=False,
    )
    write(path, data)
    return data


class CurlReader:
    """Sessão Chrome HTTP, TLS verificado, IP público fixado e redirecionamentos limitados."""

    def __init__(self, deadline):
        """Mantém cookies apenas em memória nesta variante."""
        from curl_cffi import requests

        self.session = requests.Session(impersonate="chrome", trust_env=False)
        self.deadline = deadline
        self.requests = 0
        self.events = []

    def get(self, url):
        """Baixa até oito MiB sem repetir recusas de acesso."""
        from curl_cffi import CurlOpt

        original = url
        for _ in range(4):
            if time.monotonic() >= self.deadline or self.requests >= 10:
                return FetchResult(original, None, b"", "budget_exceeded", url)
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or parts.username or parts.password:
                return FetchResult(original, None, b"", "invalid_destination", url)
            try:
                ip = public_ip(parts.hostname or "", 3)
                port = parts.port or (443 if parts.scheme == "https" else 80)
                address = f"[{ip}]" if ":" in ip else ip
                self.session.curl_options[CurlOpt.RESOLVE] = [f"{parts.hostname}:{port}:{address}"]
                chunks = []
                size = 0

                def receive(chunk, target=chunks):
                    nonlocal size
                    size += len(chunk)
                    if size > MAX_BYTES:
                        return 0
                    target.append(chunk)
                    return len(chunk)

                self.requests += 1
                response = self.session.get(
                    url,
                    timeout=min(12, max(0.1, self.deadline - time.monotonic())),
                    allow_redirects=False,
                    content_callback=receive,
                )
                self.events.append(
                    dict(
                        url=url,
                        status=response.status_code,
                        http_version=int(response.http_version),
                        bytes=size,
                        retry_after_seconds=retry_after(response.headers.get("Retry-After")),
                        cookie_count=len(self.session.cookies),
                    )
                )
                if size > MAX_BYTES:
                    return FetchResult(
                        original, response.status_code, b"", "response_too_large", url
                    )
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get(
                    "Location"
                ):
                    url = urljoin(url, response.headers["Location"])
                    continue
                return FetchResult(
                    original,
                    response.status_code,
                    b"".join(chunks),
                    None,
                    url,
                    response.headers.get("Content-Type"),
                    retry_after_seconds=retry_after(response.headers.get("Retry-After")),
                )
            except Exception as error:
                return FetchResult(
                    original, None, b"", f"{type(error).__name__}: {error}"[:300], url
                )
        return FetchResult(original, None, b"", "redirect_limit", url)

    def close(self):
        """Descarta a sessão anônima."""
        self.session.close()


class BrowserReader:
    """Chrome limpo com JavaScript, sem perfil pessoal, login ou resolução de CAPTCHA."""

    def __init__(self, browser, deadline, directory):
        """Cria um contexto isolado e orçamento de rede por variante."""
        self.context = browser.new_context(
            locale="pt-BR", viewport=dict(width=1200, height=800), service_workers="block"
        )
        self.page = self.context.new_page()
        self.deadline = deadline
        self.directory = directory
        self.requests = 0
        self.events = []
        self.validated = {}
        self.resource_requests = 0
        self.responses = []
        self.context.route("**/*", self.route)
        self.page.on("response", self.on_response)

    def route(self, route):
        """Limita sub-recursos e recusa rede privada; não modifica respostas do veículo."""
        request = route.request
        parts = urlsplit(request.url)
        if parts.scheme not in ("http", "https"):
            route.abort()
            return
        self.resource_requests += 1
        if self.resource_requests > 120 or time.monotonic() > self.deadline:
            route.abort()
            return
        if request.resource_type in ("image", "media", "font"):
            route.abort()
            return
        host = parts.hostname or ""
        if host not in self.validated:
            try:
                public_ip(host, 2)
                self.validated[host] = True
            except Exception:
                self.validated[host] = False
        if self.validated[host]:
            route.continue_()
        else:
            route.abort()

    def on_response(self, response):
        """Registra navegações principais para não confundir desafio e artigo final."""
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.responses.append(response)
        except Exception:
            pass

    def get(self, url):
        """Obtém corpo recebido e DOM renderizado, preservando ambos separadamente."""
        from playwright.sync_api import TimeoutError as BrowserTimeout

        self.requests += 1
        self.responses = []
        warning = None
        if time.monotonic() >= self.deadline:
            return FetchResult(url, None, b"", "budget_exceeded", url)
        try:
            self.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=min(16000, max(100, (self.deadline - time.monotonic()) * 1000)),
            )
        except BrowserTimeout:
            warning = "navigation_timeout"
        except Exception as error:
            warning = f"{type(error).__name__}: {error}"[:250]
        try:
            if not self.responses:
                return FetchResult(
                    url, None, b"", warning or "no_navigation_response", self.page.url
                )
            response = self.responses[-1]
            content_type = response.headers.get("content-type", "")
            if "html" in content_type:
                self.page.wait_for_timeout(
                    min(4000, max(0, (self.deadline - time.monotonic()) * 1000))
                )
                response = self.responses[-1]
                content_type = response.headers.get("content-type", "")
            raw = response.body()
            body = self.page.content().encode() if "html" in content_type else raw
            key = hashlib.sha256(url.encode()).hexdigest()
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / (key + ".received.gz")).write_bytes(gzip.compress(raw))
            if "html" in content_type:
                self.page.screenshot(path=str(self.directory / (key + ".png")), timeout=2500)
            self.events.append(
                dict(
                    url=url,
                    final_url=self.page.url,
                    status=response.status,
                    navigation_warning=warning,
                    resource_requests=self.resource_requests,
                    cookie_count=len(self.context.cookies()),
                )
            )
            if len(body) > MAX_BYTES:
                return FetchResult(url, response.status, b"", "response_too_large", self.page.url)
            return FetchResult(
                url,
                response.status,
                body,
                None,
                self.page.url,
                content_type,
                retry_after_seconds=retry_after(response.headers.get("retry-after")),
            )
        except Exception as error:
            return FetchResult(
                url, None, b"", f"{type(error).__name__}: {error}"[:300], self.page.url
            )

    def close(self):
        """Descarta cookies e contexto do teste."""
        self.context.close()


def inspect_content(target, response):
    """Separa acesso, XML válido e artigo textual, sem depender da idade da notícia."""
    result = dict(
        accessed=response.ok,
        status=response.status,
        error=response.error,
        final_url=response.final_url,
        xml_valid=False,
        text=False,
        article=False,
    )
    if not response.ok:
        return result
    if target["kind"] == "xml":
        try:
            doc = parse_document(response.content, response.final_url or response.url, MAX_BYTES)
            result.update(
                xml_valid=True,
                kind=doc["kind"],
                entries=len(doc["entries"]),
                children=len(doc["children"]),
            )
        except ValueError as error:
            result["format_error"] = str(error)[:200]
    elif target["kind"] == "article":
        now = datetime.now(UTC)
        audit = audit_page(
            PageSnapshot(
                url=response.url,
                status=response.status,
                content_type=response.content_type,
                final_url=response.final_url,
                html=response.content,
            ),
            since=datetime.min.replace(tzinfo=UTC),
            until=now,
            now=now,
        )
        result.update(
            article=audit["artigo_acessivel"] and not audit["audio_dedicado"],
            text=audit["texto_util_confirmado"],
            chars=audit["caracteres_texto"],
            title=audit["titulo"],
            published_at=audit["publicado_em"],
            reason=audit["motivos"],
        )
    return result


def evaluate(target, variant, browser, pinned, global_deadline):
    """Executa uma variante e amostra no máximo uma notícia, com dois níveis de XML."""
    path = OUT / "medicoes" / f"{target['id']:02d}-{variant}.json"
    if path.exists():
        return json.loads(path.read_text())
    started = time.monotonic()
    deadline = min(global_deadline, started + 45)
    directory = OUT / "corpos" / f"{target['id']:02d}" / variant
    directory.mkdir(parents=True, exist_ok=True)
    row = dict(target=target, variant=variant, started_at=stamp(), reads=[], warnings=[])
    if variant.startswith("http_"):
        reader = TestReader(
            [target["source_url"], target["url"]],
            lambda _: None,
            seconds=max(0.1, deadline - started),
            requests=10,
            max_bytes=MAX_BYTES,
            sessions=HttpSessions(),
        )
    elif variant == "chromium":
        reader = BrowserReader(browser, deadline, directory)
    else:
        reader = CurlReader(deadline)

    def read(url, kind, role):
        if len(row["reads"]) >= 6 or time.monotonic() >= deadline:
            raise TimeoutError("budget_exceeded")
        begin = time.monotonic()
        response = reader.get(url)
        key = hashlib.sha256(url.encode()).hexdigest()
        (directory / (key + ".body.gz")).write_bytes(gzip.compress(response.content))
        assessment = inspect_content(dict(target, kind=kind), response)
        row["reads"].append(
            dict(
                url=url,
                role=role,
                elapsed_seconds=round(time.monotonic() - begin, 3),
                bytes=len(response.content),
                sha256=hashlib.sha256(response.content).hexdigest(),
                retry_after_seconds=response.retry_after_seconds,
                **assessment,
            )
        )
        return response, assessment

    try:
        if variant == "curl_chrome_home":
            read(target["source_url"], "catalog_page", "home")
        response, assessment = read(target["url"], target["kind"], "target")
        if target["kind"] == "xml" and assessment.get("xml_valid"):
            site = Site.create(
                name=target["source"],
                url=target["source_url"],
                region="",
                country="",
                country_code="",
                language="",
                source_type="",
            )
            depth = 0
            while depth <= 2:
                doc = parse_document(
                    response.content, response.final_url or response.url, MAX_BYTES
                )
                entries = [
                    u
                    for u in doc["entries"]
                    if site.owns(u)
                    and not any(
                        term in u.lower() for term in ["/video/", "/videos/", "/podcast", "/plus/"]
                    )
                ]
                if entries:
                    selected = pinned.get(target["id"])
                    if selected is None:
                        selected = max(
                            entries, key=lambda u: doc["dates"].get(u, doc["hints"].get(u, ""))
                        )
                        pinned[target["id"]] = selected
                    read(selected, "article", "article")
                    break
                children = [u for u in doc["children"] if site.owns_host(u)]
                if not children or depth == 2:
                    break
                selected = max(children, key=lambda u: (doc["child_dates"].get(u, ""), u))
                response, child = read(selected, "xml", "child_xml")
                if not child.get("xml_valid"):
                    break
                depth += 1
    except Exception as error:
        row["warnings"].append(f"{type(error).__name__}: {error}"[:300])
    finally:
        row.update(
            finished_at=stamp(),
            elapsed_seconds=round(time.monotonic() - started, 3),
            document_requests=reader.requests,
            transport_events=getattr(reader, "events", []),
        )
        row["resource_requests"] = getattr(reader, "resource_requests", 0)
        if hasattr(reader, "close"):
            reader.close()
    root = next((r for r in row["reads"] if r["role"] == "target"), {})
    article = next(
        (r for r in row["reads"] if r["role"] == "article"),
        root if target["kind"] == "article" else {},
    )
    row.update(
        root_status=root.get("status"),
        root_accessed=root.get("accessed", False),
        xml_valid=root.get("xml_valid", False),
        article_text=bool(article.get("article") and article.get("text")),
        useful=bool(root.get("accessed") and (article.get("article") and article.get("text"))),
    )
    write(path, row)
    with PRINT_LOCK:
        print(
            stamp(),
            target["id"],
            target["source"],
            variant,
            row["root_status"],
            "XML",
            row["xml_valid"],
            "TEXTO",
            row["article_text"],
            row["elapsed_seconds"],
            "s",
            flush=True,
        )
    return row


def group_worker(targets, global_deadline):
    """Serializa todas as variantes por veículo; três veículos no máximo em paralelo."""
    from playwright.sync_api import sync_playwright

    rows = []
    pinned = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/opt/google/chrome/chrome", headless=True
        )
        for target in targets:
            for variant in ["http_current", "curl_chrome", "curl_chrome_home", "chromium"]:
                if time.monotonic() >= global_deadline:
                    break
                rows.append(evaluate(target, variant, browser, pinned, global_deadline))
                # Requisições do mesmo veículo são seriadas e espaçadas.
                time.sleep(1)
        for target in targets:
            if time.monotonic() >= global_deadline:
                break
            rows.append(evaluate(target, "http_recheck", browser, pinned, global_deadline))
            time.sleep(1)
        browser.close()
    return rows


def main():
    """Executa as cem medições planejadas, com checkpoints e teto de vinte minutos."""
    plan = manifest()
    groups = defaultdict(list)
    for target in plan["targets"]:
        groups[target["source_id"]].append(target)
    deadline = time.monotonic() + 1200
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed(
            [pool.submit(group_worker, targets, deadline) for targets in groups.values()]
        ):
            results.extend(future.result())
    write(OUT / "resultado.json", dict(finished_at=stamp(), measurements=results))
    print("CONCLUÍDO", len(results), "medições", flush=True)


if __name__ == "__main__":
    main()
