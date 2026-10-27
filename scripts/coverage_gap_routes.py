"""Uma correção limitada de rotas oficiais, separada da cobertura da rodada original."""

from __future__ import annotations

import gzip
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from lxml import html
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from audit_frontpage_coverage import close_reader, store_body, write
from buscador_midias.catalog.http_transport import make_reader
from buscador_midias.catalog.mechanisms.parsing import Document, parse_document
from buscador_midias.catalog.postgres import WRITE_LOCK, connect_postgres
from buscador_midias.catalog.store import CatalogStore
from buscador_midias.catalog.testing import run_test
from buscador_midias.domain.site import Site
from buscador_midias.domain.url import canonicalize_url, url_hash_candidates


def route_candidates(result: dict[str, Any], output: Path, site: Site) -> list[str]:
    """Somente links publicados nas páginas congeladas e dois endereços convencionais."""
    found: dict[str, None] = {}
    for page in result.get("reference_pages", []):
        if not page.get("proof"):
            continue
        body = gzip.decompress((output / page["proof"]).read_bytes())
        tree: Any = html.document_fromstring(body)
        for element in tree.xpath("//link[@href]|//a[@href]"):
            href = element.get("href", "")
            mime = element.get("type", "").lower()
            if not (
                "rss" in mime
                or "atom" in mime
                or any(word in href.lower() for word in ("rss", "sitemap", "/feed"))
            ):
                continue
            address = urljoin(page["url"], href)
            if site.owns(address):
                found.setdefault(address, None)
    for path in ("sitemap.xml", "feed/"):
        address = site.url.rstrip("/") + "/" + path
        if site.owns(address):
            found.setdefault(address, None)
    return list(found)[:8]


def matching_entries(document: Document, gaps: list[dict[str, Any]]) -> list[str]:
    """Conferência de identidade, nunca similaridade de manchete."""
    keys = {key for address in document["entries"] for key in url_hash_candidates(address)}
    return [gap["url"] for gap in gaps if keys.intersection(gap.get("hashes", []))]


def add_verified_route(
    source: dict[str, Any],
    route: dict[str, Any],
    test: dict[str, Any],
    proof: dict[str, Any],
) -> dict[str, Any]:
    """Insere apenas rota nova em fonte ativa e inalterada; nunca restaura exclusões."""
    if (
        not proof.get("matched_urls")
        or not test.get("usable_for_news")
        or test.get("source_id") != source["id"]
        or test.get("source_url") != source["url"]
        or test.get("url") != route["url"]
        or test.get("requested_kind") != route["kind"]
        or test.get("access_transport", "direct") != route["transport"]
    ):
        return dict(decision="proof_or_configuration_mismatch")
    with connect_postgres() as db:
        db.row_factory = dict_row
        db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
        parent = db.execute(
            "SELECT * FROM midias_manager.sites WHERE id=%s FOR UPDATE", (source["id"],)
        ).fetchone()
        if not parent or parent["deleted_at"] or not parent["active"]:
            return dict(decision="preserved_inactive_source")
        if parent["version"] != source["version"] or parent["url"] != source["url"]:
            return dict(decision="catalog_changed")
        # Inclui inativos e excluídos: sua intenção anterior é preservada.
        existing = db.execute("SELECT id,url FROM midias_manager.links").fetchall()
        if any(canonicalize_url(row["url"]) == canonicalize_url(route["url"]) for row in existing):
            return dict(decision="existing_route_preserved")
        model = db.execute(
            "SELECT version FROM midias_manager.mechanisms WHERE model=%s", (test["model"],)
        ).fetchone()
        if (
            not model
            or model["version"] != test["model_version"]
            or not test.get("usable_for_news")
        ):
            return dict(decision="test_or_model_changed")
        evidence = dict(CatalogStore._test_evidence(test), coverage_audit=proof)
        row = db.execute(
            "INSERT INTO midias_manager.links "
            "(source_id,url,kind,model,active,access_transport,last_test,evidence,origin,"
            "search_interval_seconds,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,1,%s,%s,%s,'coverage_gap',14400,now(),now()) RETURNING *",
            (
                source["id"],
                route["url"],
                route["kind"],
                test["model"],
                route["transport"],
                Jsonb(test),
                Jsonb(evidence),
            ),
        ).fetchone()
        assert row is not None
        # Mesma trilha do gerenciador, com valores serializáveis para o JSONB.
        after = json.loads(json.dumps(row, default=str))
        db.execute(
            "INSERT INTO midias_manager.events"
            "(entity,entity_id,action,before_json,after_json,happened_at) "
            "VALUES('endpoint',%s,'coverage_gap_create',NULL,%s,now())",
            (row["id"], Jsonb(after)),
        )
        db.execute(
            "INSERT INTO midias_manager.link_test_results"
            "(link_id,model,test_id,model_version,observed_at,parameters,result) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                row["id"],
                test["model"],
                test["test_id"],
                test["model_version"],
                test["observed_at"],
                Jsonb(test["parameters"]),
                Jsonb(test),
            ),
        )
        db.execute(
            "INSERT INTO midias_manager.ingest_daily_routes(link_id,purpose,reason) "
            "VALUES(%s,'updates',%s)",
            (
                row["id"],
                "Contém notícia da janela diária na referência congelada; "
                "XML, texto e uma conferência confirmados pela auditoria de cobertura.",
            ),
        )
        return dict(decision="created", link_id=row["id"])


def correct_source(
    result: dict[str, Any], manifest: dict[str, Any], output: Path
) -> dict[str, Any]:
    """Até oito candidatos, oito XMLs por candidato e 180 segundos por fonte."""
    target = result["target"]
    path = output / "corrections" / f"{target['source_id']}.json"
    if path.exists():
        return dict(json.loads(path.read_text()))
    gaps = [a for a in result["articles"] if a.get("diagnostic") == "discovery_gap"]
    record: dict[str, Any] = dict(source_id=target["source_id"], gaps=len(gaps), routes=[])
    if not gaps:
        record["reason"] = "no_proven_discovery_gap"
        write(path, record)
        return record
    snapshot = json.loads((output / "catalog-before.json").read_text())
    source = next(s for s in snapshot["sources"] if s["id"] == target["source_id"])
    site = Site.create(
        name=source["name"],
        url=source["url"],
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )
    deadline = time.monotonic() + 180
    reader = make_reader(
        [site.url],
        lambda _: None,
        transport=target["transport"],
        seconds=180,
        requests=96,
        max_bytes=4 * 1024 * 1024,
    )
    try:
        for address in route_candidates(result, output, site):
            if time.monotonic() >= deadline - 10:
                record["limited"] = True
                break
            attempt: dict[str, Any] = dict(url=address, matches=[], verified=False)
            record["routes"].append(attempt)
            try:
                response = reader.get(address)
                attempt["http_status"] = response.status
                if not response.ok or not site.owns(response.final_url or address):
                    continue
                root = parse_document(
                    response.content, response.final_url or address, 4 * 1024 * 1024
                )
                attempt["proof"] = store_body(output, response.content)
                model = (
                    "rss"
                    if root["kind"] == "feed"
                    else "sitemap_tree"
                    if root["index"]
                    else "news_sitemap"
                    if root["news"]
                    else "sitemap"
                )
                documents = [(address, root)]
                # Não expande recursivamente calendários/arquivos sem fim.
                for child in reversed(root["children"][-7:]):
                    if not site.owns(child):
                        continue
                    fetched = reader.get(child)
                    if fetched.ok and site.owns(fetched.final_url or child):
                        documents.append(
                            (child, parse_document(fetched.content, child, 4 * 1024 * 1024))
                        )
                matches = {url for _, doc in documents for url in matching_entries(doc, gaps)}
                attempt["matches"] = sorted(matches)
                if not matches:
                    continue
                settings = next(m for m in snapshot["models"] if m["model"] == model)
                parameters = dict(
                    settings["parameters"],
                    lookback_days=0,
                    timeout_seconds=20,
                    article_samples=3,
                    max_requests=16,
                    max_response_mib=4,
                )
                config = dict(
                    test_id=uuid.uuid4().hex,
                    url=address,
                    kind=root["kind"],
                    model=model,
                    model_version=settings["version"],
                    parameters=parameters,
                    access_transport=target["transport"],
                    bright_data=False,
                )
                # Segunda passagem: o teste normal precisa confirmar notícia textual.
                parameters["timeout_seconds"] = max(5, min(20, int(deadline - time.monotonic())))
                test = run_test(config, source, lambda _: None)
                attempt["test"] = test
                if not test.get("usable_for_news"):
                    continue
                confirmed = set()
                for url, _ in documents:
                    fetched = reader.get(url)
                    if fetched.ok and site.owns(fetched.final_url or url):
                        doc = parse_document(fetched.content, url, 4 * 1024 * 1024)
                        confirmed.update(matching_entries(doc, gaps))
                attempt["verified_matches"] = sorted(matches & confirmed)
                attempt["verified"] = bool(matches & confirmed)
                if attempt["verified"]:
                    proof = dict(
                        run_id=manifest["run_id"],
                        matched_urls=attempt["verified_matches"],
                        reference_file=f"references/{source['id']}.json",
                        xml_proof=attempt["proof"],
                        verification_passes=1,
                    )
                    if root["index"]:
                        attempt["application"] = dict(
                            decision="index_collection_plan_review",
                            reason="A amostra confirma notícias, mas não calibra todos os ramos "
                            "temporais do índice para as próximas buscas diárias.",
                        )
                    else:
                        attempt["application"] = add_verified_route(
                            source,
                            dict(url=address, kind=root["kind"], transport=target["transport"]),
                            test,
                            proof,
                        )
            except Exception as error:
                attempt["error"] = type(error).__name__
    finally:
        close_reader(reader)
    record["recovered_urls"] = sorted(
        {url for route in record["routes"] for url in route.get("verified_matches", [])}
    )
    record["note"] = "Rotas para buscas futuras; não altera a cobertura da rodada original."
    write(path, record)
    return record


def correct_and_verify(
    results: list[dict[str, Any]], manifest: dict[str, Any], output: Path
) -> None:
    """Uma passagem por fonte; checkpoint impede repetição infinita."""
    import audit_frontpage_coverage

    audit_frontpage_coverage.PROOF_BYTES = sum(
        path.stat().st_size for path in (output / "html").glob("*.gz")
    )
    corrected = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(correct_source, result, manifest, output) for result in results]
        for future in as_completed(futures):
            try:
                corrected.append(future.result())
            except Exception as error:
                corrected.append(dict(error=type(error).__name__))
            write(output / "corrections.json", corrected)
    audit_frontpage_coverage.report(results, manifest, output)
