"""Auditoria finita de capa após uma rodada, sem IA nem escrita no corpus."""

from __future__ import annotations

import argparse
import copy
import csv
import fcntl
import gzip
import hashlib
import html as escaping
import json
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any

from lxml import html
from psycopg.rows import dict_row

from buscador_midias.analysis.coverage import (
    diagnose,
    editorial_sections,
    eligible,
    frontpage_candidates,
    metrics,
)
from buscador_midias.catalog.http_transport import make_reader
from buscador_midias.catalog.postgres import connect_postgres
from buscador_midias.catalog.reader import TestLimitError
from buscador_midias.domain.site import Site
from buscador_midias.domain.url import url_hash_candidates
from buscador_midias.extraction.html import extract_article
from buscador_midias.extraction.reliability import PageSnapshot, assess_evidence
from buscador_midias.extraction.sample_audit import audit_page

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "partial"}
PROOF_LOCK = threading.Lock()
PROOF_BYTES = 0
MAX_PROOF_BYTES = 1024 * 1024 * 1024

LIMITS = dict(
    workers=8,
    source_seconds=120,
    source_requests=96,
    candidates=60,
    reference_articles=25,
    sections=2,
    bytes=4 * 1024 * 1024,
)


def close_reader(reader: Any) -> None:
    """Curl tem sessão própria; o leitor HTTP direto usa o pool compartilhado."""
    close = getattr(reader, "close", None)
    if close is not None:
        close()


def write(path: Path, value: Any) -> None:
    """Checkpoint atômico; corpos capturados ficam separados, em gzip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, default=str, indent=2))
    temporary.replace(path)


def configure_environment() -> None:
    """Lê apenas credenciais PG conhecidas; não executa arquivo de ambiente."""
    path = Path(os.environ.get("KYX_ENV", ROOT.parent / "KyX-NovoDD-backend/.env"))
    keys = {"DB_MIDIAS_IA_" + part for part in ("HOST", "PORT", "DATABASE", "USERNAME", "PASSWORD")}
    if path.is_file():
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key in keys:
                os.environ.setdefault(key, value.strip().strip("\"'"))
    os.environ["DB_MIDIAS_IA_HOST"] = os.environ.get("CORPUS_DB_HOST", "127.0.0.1")


def read_db(query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Cada operação usa transação explicitamente somente leitura."""
    with connect_postgres() as db:
        db.row_factory = dict_row
        db.execute("SET TRANSACTION READ ONLY")
        return list(db.execute(query, parameters).fetchall())


def wait_for_run(run_id: str, output: Path) -> dict[str, Any]:
    """Aguarda somente o ID congelado; não chama maintain nem inicia coleta."""
    while True:
        rows = read_db("SELECT * FROM midias_manager.ingest_runs WHERE id=%s", (run_id,))
        if not rows:
            raise ValueError("Rodada não encontrada")
        run = rows[0]
        write(
            output / "status.json",
            dict(state="waiting_for_run", run_id=run_id, observed_at=datetime.now(UTC)),
        )
        if run["state"] in TERMINAL and run["finished_at"] is not None:
            return run
        if run["state"] != "running":
            raise ValueError("Rodada não terminou em estado auditável")
        time.sleep(30)


def store_body(directory: Path, body: bytes) -> str:
    """Conserva o HTML obtido como prova independente, sem alterar evidências do corpus."""
    digest = hashlib.sha256(body).hexdigest()
    path = directory / "html" / (digest + ".html.gz")
    global PROOF_BYTES
    packed = gzip.compress(body, compresslevel=3)
    with PROOF_LOCK:
        if not path.exists():
            if (
                PROOF_BYTES + len(packed) > MAX_PROOF_BYTES
                or shutil.disk_usage(directory).free < 512 * 1024 * 1024
            ):
                raise OSError("audit_storage_budget")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".partial")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(packed)
                os.replace(temporary, path)
                PROOF_BYTES += len(packed)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return str(path.relative_to(directory))


def aliases(row: dict[str, Any], body: bytes, site: Site) -> list[str]:
    """Aceita canonical somente no escopo editorial, com paridade de hash do corpus."""
    addresses = [row["url"], row.get("final_url") or row["url"]]
    tree: Any = html.document_fromstring(body)
    for address in tree.xpath(
        '//link[contains(concat(" ",normalize-space(@rel)," ")," canonical ")]/@href'
    ):
        from urllib.parse import urljoin

        address = urljoin(addresses[-1], address)
        if site.owns(address):
            addresses.append(address)
    return list(dict.fromkeys(h for u in addresses for h in url_hash_candidates(u)))


def compare(rows: list[dict[str, Any]], source_id: int, run_id: str, cutoff: str) -> None:
    """Compara em lote; corpus preexistente conta, outro horário posterior não conta."""
    keys = list({key for row in rows for key in row.get("hashes", [])})
    if not keys:
        return
    corpus = read_db(
        "SELECT id,url_hash,created_at,evidencia_status FROM public.midias_ia_articles "
        "WHERE url_hash=ANY(%s) AND created_at<=%s",
        (keys, cutoff),
    )
    origins = read_db(
        "SELECT DISTINCT o.hash,(p.snapshot->'source'->>'id')::bigint source_id "
        "FROM midias_manager.ingest_occurrences o JOIN midias_manager.ingest_polls p "
        "ON p.id=o.poll_id WHERE o.run_id=%s AND o.hash=ANY(%s)",
        (run_id, keys),
    )
    jobs = read_db(
        "SELECT DISTINCT ON(hash) hash,stage,state,outcome,result FROM midias_manager.ingest_jobs "
        "WHERE run_id=%s AND hash=ANY(%s) ORDER BY hash,generation DESC,"
        "CASE stage WHEN 'persist' THEN 6 WHEN 'evidence' THEN 5 WHEN 'context' THEN 4 "
        "WHEN 'fetch' THEN 3 ELSE 2 END DESC,created_at DESC",
        (run_id, keys),
    )
    stages = read_db(
        "SELECT hash,stage,state,outcome FROM midias_manager.ingest_jobs "
        "WHERE run_id=%s AND hash=ANY(%s) ORDER BY generation,created_at",
        (run_id, keys),
    )
    run = read_db("SELECT created_at FROM midias_manager.ingest_runs WHERE id=%s", (run_id,))[0]
    c_by = {c["url_hash"]: c for c in corpus}
    j_by = {j["hash"]: j for j in jobs}
    local = {o["hash"] for o in origins if o["source_id"] == source_id}
    all_origins = {o["hash"] for o in origins}
    for row in rows:
        hashes = row.get("hashes", [])
        row["discovered"] = any(h in all_origins for h in hashes)
        row["discovered_by_source"] = any(h in local for h in hashes)
        row["stages"] = {
            item["stage"]: dict(state=item["state"], outcome=item["outcome"])
            for item in stages
            if item["hash"] in hashes
        }
        c = next((c_by[h] for h in hashes if h in c_by), None)
        j = next((j_by[h] for h in hashes if h in j_by), None)
        if c:
            row.update(
                corpus_id=c["id"],
                corpus_new=c["created_at"] >= run["created_at"],
                evidence_status=c["evidencia_status"],
            )
        if j:
            row.update(
                stage=j["stage"],
                job_state=j["state"],
                outcome=j["outcome"],
                article_status=(j["result"] or {}).get("status"),
                coverage_partial=(j["result"] or {}).get("coverage_partial"),
            )
        row["diagnostic"] = diagnose(row)


def audit_source(target: dict[str, Any], manifest: dict[str, Any], output: Path) -> dict[str, Any]:
    """Uma capa, até 200 candidatas, orçamento único; sem links de RSS como referência."""
    path = output / "sources" / (str(target["source_id"]) + ".json")
    if path.exists():
        return dict(json.loads(path.read_text()))
    start = datetime.now(UTC)
    deadline = time.monotonic() + LIMITS["source_seconds"]
    requests_used = 0
    result: dict[str, Any] = dict(
        target=target,
        observed_at=start.isoformat(),
        articles=[],
        state="inconclusive",
        limits=LIMITS,
    )
    site = Site.create(
        name=target["name"],
        url=target["homepage"],
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )
    # Tenta o perfil comprovado e HTTP direto, sem fallback pago ou varredura de perfis.
    transports = list(dict.fromkeys([target["transport"], "direct"]))
    reader = None
    try:
        if target["source_id"] == 25 and "record.com.br" in target["homepage"]:
            result["reason"] = "catalog_identity_review_book_publisher_not_news_network"
            return result
        homepage = None
        for transport in transports:
            if reader:
                requests_used += reader.requests
                close_reader(reader)
            reader = make_reader(
                [target["homepage"]],
                lambda _: None,
                transport=transport,
                seconds=max(0.01, deadline - time.monotonic()),
                requests=max(1, LIMITS["source_requests"] - requests_used),
                max_bytes=LIMITS["bytes"],
            )
            candidate = reader.get(target["homepage"])
            result.setdefault("homepage_attempts", []).append(
                dict(transport=transport, status=candidate.status, error=candidate.error)
            )
            if candidate.ok and site.owns(candidate.final_url or target["homepage"]):
                assessment = assess_evidence(
                    PageSnapshot(
                        target["homepage"],
                        candidate.status,
                        candidate.content_type,
                        candidate.final_url,
                        candidate.content,
                    ),
                    expected_headline="",
                    now=start,
                )
                if not {"challenge_page", "error_page", "http_status"} & set(assessment.reasons):
                    homepage = candidate
                    break
        if homepage is None or reader is None:
            result["reason"] = "homepage_inaccessible_or_challenge"
            return result
        result["homepage_proof"] = store_body(output, homepage.content)
        candidates = frontpage_candidates(
            homepage.content, homepage.final_url or target["homepage"], site
        )
        groups = [candidates]
        result["reference_pages"] = [
            dict(url=homepage.final_url or target["homepage"], proof=result["homepage_proof"])
        ]
        sections = editorial_sections(
            homepage.content,
            homepage.final_url or target["homepage"],
            site,
            LIMITS["sections"],
        )
        # Cada página de referência é congelada; a conferência não troca a amostra.
        for section in sections:
            try:
                response = reader.get(section)
                section_page = dict(url=section, http_status=response.status, error=response.error)
                if response.ok and site.owns(response.final_url or section):
                    section_page["proof"] = store_body(output, response.content)
                    groups.append(frontpage_candidates(response.content, section, site))
                result["reference_pages"].append(section_page)
            except (TestLimitError, OSError, ValueError) as error:
                result["reference_pages"].append(dict(url=section, error=type(error).__name__))
        candidates = list(
            {c["url"]: c for batch in zip_longest(*groups) for c in batch if c is not None}.values()
        )
        result.update(
            candidates_found=len(candidates), truncated=len(candidates) > LIMITS["candidates"]
        )
        since = datetime.fromisoformat(manifest["run"]["since"])
        until = datetime.fromisoformat(manifest["run"]["until"])
        seen: set[str] = set()
        for candidate in candidates[: LIMITS["candidates"]]:
            row: dict[str, Any] = dict(candidate, reference="access_inconclusive")
            try:
                response = reader.get(candidate["url"])
                row.update(
                    http_status=response.status, error=response.error, final_url=response.final_url
                )
                if response.ok and site.owns(response.final_url or row["url"]):
                    body = response.content
                    row["proof"] = store_body(output, body)
                    page = PageSnapshot(
                        row["url"], response.status, response.content_type, response.final_url, body
                    )
                    page_assessment = audit_page(page, since=since, until=until, now=start)
                    row["page_category"] = page_assessment["categoria"]
                    row["hashes"] = aliases(row, body, site)
                    if seen.intersection(row["hashes"]):
                        row["reference"] = "duplicate"
                    else:
                        if page_assessment["texto_util_confirmado"]:
                            seen.update(row["hashes"])
                            article = extract_article(
                                body, content_type=response.content_type, max_chars=1000, now=start
                            )
                            row.update(
                                reference=eligible(article.published_at, since, until),
                                published_at=article.published_at,
                            )
                        else:
                            row["reference"] = "text_not_confirmed"
            except TestLimitError:
                result["truncated"] = True
                result["reason"] = "source_budget_reached"
                row["reference"] = "budget_inconclusive"
                result["articles"].append(row)
                break
            except (OSError, ValueError) as error:
                row["error"] = type(error).__name__
            result["articles"].append(row)
            if (
                sum(a["reference"] == "in_window" for a in result["articles"])
                >= LIMITS["reference_articles"]
            ):
                result["truncated"] = True
                result["reason"] = "reference_sample_limit"
                break
        if not manifest.get("capture_only"):
            compare(
                result["articles"],
                int(target["source_id"]),
                manifest["run"]["id"],
                manifest["comparison_at"],
            )
        result["state"] = (
            "sampled"
            if any(r["reference"] == "in_window" for r in result["articles"])
            else "inconclusive"
        )
        result["metrics"] = metrics(result["articles"], compared=not manifest.get("capture_only"))
        if not candidates:
            result["reason"] = "no_editorial_links_in_http_html_possible_javascript"
        return result
    except Exception as error:
        result["reason"] = "audit_internal_error:" + type(error).__name__
        return result
    finally:
        if reader:
            result["requests_total"] = requests_used + reader.requests
            close_reader(reader)
        result["finished_at"] = datetime.now(UTC).isoformat()
        write(path, result)


def report(results: list[dict[str, Any]], manifest: dict[str, Any], output: Path) -> None:
    """Exporta resumo, CSV por notícia e relatório navegável com links de origem."""
    rows = [
        dict(source_id=r["target"]["source_id"], source=r["target"]["name"], **a)
        for r in results
        for a in r["articles"]
    ]
    summary = dict(
        run=manifest["run"],
        comparison_at=manifest["comparison_at"],
        audited_sites=len(results),
        comparable_sites=sum(r["state"] == "sampled" for r in results),
        sites_with_limits=sum(bool(r.get("truncated")) for r in results),
        metrics=metrics(rows, compared=not manifest.get("capture_only")),
        note="Amostra congelada de capa/editorias; não é cobertura integral do site.",
        provisional=bool(manifest.get("capture_only")),
        sample_inconclusive_articles=sum(
            a.get("reference")
            in ("access_inconclusive", "date_unknown", "text_not_confirmed", "budget_inconclusive")
            for a in rows
        ),
        selection=("Recorte editorial: 60 Brasil e 60 internacionais; não é ranking de audiência"),
    )
    write(output / "summary.json", summary)
    site_columns = [
        "source_id",
        "source",
        "state",
        "reference_articles",
        "corpus",
        "discovered",
        "corpus_percent",
        "policy_excluded",
        "limited",
        "reason",
    ]
    with (output / "sites.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=site_columns, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            writer.writerow(
                dict(
                    source_id=result["target"]["source_id"],
                    source=result["target"]["name"],
                    state=result["state"],
                    limited=bool(result.get("truncated")),
                    reason=result.get("reason", ""),
                    **metrics(result["articles"], compared=not manifest.get("capture_only")),
                )
            )
    columns = [
        "source_id",
        "source",
        "url",
        "headline",
        "reference",
        "published_at",
        "diagnostic",
        "discovered",
        "discovered_by_source",
        "corpus_id",
        "corpus_new",
        "stage",
        "job_state",
        "outcome",
        "article_status",
        "evidence_status",
        "http_status",
        "error",
    ]
    with (output / "articles.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    esc = escaping.escape
    corrections_path = output / "corrections.json"
    corrections = json.loads(corrections_path.read_text()) if corrections_path.exists() else []
    corrected_by_source = {r["source_id"]: r for r in corrections if "source_id" in r}
    cards = []
    for result in sorted(results, key=lambda r: r["target"]["name"]):
        target = result["target"]
        m = metrics(result["articles"], compared=not manifest.get("capture_only"))
        lines = "".join(
            '<tr><td><a href="'
            + esc(a["url"], quote=True)
            + '">'
            + esc(a["headline"])
            + "</a></td><td>"
            + esc(a.get("diagnostic", a["reference"]))
            + "</td><td>"
            + str(a.get("corpus_id", ""))
            + "</td><td>"
            + esc(json.dumps(a.get("stages", {}), ensure_ascii=False))
            + "</td></tr>"
            for a in result["articles"]
        )
        cards.append(
            "<details><summary>"
            + esc(target["name"])
            + " — "
            + (
                "referência em coleta; "
                if manifest.get("capture_only")
                else f"{m['corpus']}/{m['reference_articles']} no corpus; "
            )
            + ("inconclusivo; " if m["corpus_percent"] is None else "")
            + esc(result["state"])
            + "</summary><pre>"
            + esc(
                json.dumps(
                    {k: v for k, v in result.items() if k not in ("articles", "metrics")},
                    default=str,
                    ensure_ascii=False,
                    indent=2,
                )
            )
            + "</pre><table><tr><th>Notícia de referência</th><th>Diagnóstico</th>"
            "<th>ID corpus</th><th>Etapas da rodada</th></tr>"
            + lines
            + "</table>"
            + (
                "<h3>Correção e conferência de rotas para buscas futuras</h3><pre>"
                + esc(
                    json.dumps(
                        corrected_by_source[target["source_id"]],
                        ensure_ascii=False,
                        default=str,
                        indent=2,
                    )
                )
                + "</pre>"
                if target["source_id"] in corrected_by_source
                else ""
            )
            + "</details>"
        )
    (output / "report.html").write_text(
        '<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Cobertura de capas</title><style>body{font:16px system-ui;max-width:1300px;margin:32px auto;padding:16px}details{border:1px solid #ddd;padding:16px;margin:12px 0}td,th{text-align:left;padding:8px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap}</style><h1>Cobertura das capas — 120 fontes prioritárias</h1><p>Comparação observacional após a rodada. Ausência de prova, bloqueio, publicação posterior e limite de amostra não contam como zero de cobertura.</p><pre>'  # noqa: E501 - documento HTML estático
        + "</pre><h2>"
        + (
            f"Coletando referências: {len(results)}/120 fontes. Cobertura ainda não calculada."
            if manifest.get("capture_only")
            else "Comparação da rodada concluída"
        )
        + "</h2><p>Correções de rotas são registradas separadamente e não aumentam "
        "retroativamente o resultado desta rodada.</p><pre>"
        + esc(json.dumps(summary, default=str, ensure_ascii=False, indent=2))
        + "</pre>"
        + "".join(cards)
        + "</html>"
    )


def frozen_audit(manifest: dict[str, Any], output: Path, *, correct_routes: bool) -> int:
    """Captura uma vez, espera a rodada e compara a mesma referência sem nova navegação."""
    run_id = manifest["run_id"]
    current = read_db("SELECT * FROM midias_manager.ingest_runs WHERE id=%s", (run_id,))[0]
    manifest.update(
        run=json.loads(json.dumps(current, default=str)),
        comparison_at=datetime.now(UTC).isoformat(),
        capture_only=True,
        reference_mode="frozen_homepage_and_sections",
        correction_passes=int(correct_routes),
        limits=LIMITS,
    )
    snapshot_path = output / "catalog-before.json"
    if not snapshot_path.exists():
        write(
            snapshot_path,
            dict(
                sources=read_db(
                    "SELECT id,name,url,version,active,deleted_at FROM midias_manager.sites "
                    "WHERE id=ANY(%s)",
                    ([t["source_id"] for t in manifest["targets"]],),
                ),
                models=read_db("SELECT model,version,parameters FROM midias_manager.mechanisms"),
            ),
        )
    write(output / "manifest.json", manifest)
    results = []
    write(
        output / "status.json",
        dict(
            state="freezing_references",
            completed_sites=0,
            total=len(manifest["targets"]),
            run_id=run_id,
        ),
    )

    def capture(target: dict[str, Any]) -> dict[str, Any]:
        frozen = output / "references" / f"{target['source_id']}.json"
        if frozen.exists():
            return dict(json.loads(frozen.read_text()))
        result = audit_source(target, manifest, output)
        write(frozen, result)
        return result

    with ThreadPoolExecutor(max_workers=LIMITS["workers"]) as pool:
        futures = [pool.submit(capture, target) for target in manifest["targets"]]
        for future in as_completed(futures):
            try:
                results.append(future.result())
                report(results, manifest, output)
            except Exception as error:
                write(
                    output / "status.json",
                    dict(
                        state="audit_error",
                        error=repr(error),
                        completed_sites=len(results),
                        run_id=run_id,
                    ),
                )
                raise
            write(
                output / "status.json",
                dict(
                    state="freezing_references",
                    completed_sites=len(results),
                    total=120,
                    run_id=run_id,
                ),
            )
    run = wait_for_run(run_id, output)
    manifest.update(
        run=json.loads(json.dumps(run, default=str)),
        comparison_at=str(run["finished_at"]),
        capture_only=False,
    )
    polls = read_db(
        "SELECT (snapshot->'source'->>'id')::bigint source_id,count(*) n "
        "FROM midias_manager.ingest_polls WHERE run_id=%s GROUP BY 1",
        (run_id,),
    )
    counts = {p["source_id"]: p["n"] for p in polls}
    write(output / "manifest.json", manifest)
    final = []
    for frozen in results:
        result = copy.deepcopy(frozen)
        result["target"]["links_in_run"] = counts.get(result["target"]["source_id"], 0)
        compare(
            result["articles"], result["target"]["source_id"], run_id, manifest["comparison_at"]
        )
        result["metrics"] = metrics(result["articles"])
        final.append(result)
        write(output / "sources" / f"{result['target']['source_id']}.json", result)
    report(final, manifest, output)
    gaps = [
        dict(source_id=r["target"]["source_id"], source=r["target"]["name"], **a)
        for r in final
        for a in r["articles"]
        if a.get("diagnostic")
        in (
            "discovery_gap",
            "processing_failure",
            "extraction_or_date_gap",
            "processing_unresolved",
        )
    ]
    write(output / "gaps.json", gaps)
    if correct_routes:
        from coverage_gap_routes import correct_and_verify

        write(output / "status.json", dict(state="correcting_gap_routes", run_id=run_id))
        correct_and_verify(final, manifest, output)
    write(
        output / "status.json",
        dict(
            state="completed",
            completed_sites=len(final),
            run_id=run_id,
            finished_at=datetime.now(UTC),
            correction_passes=int(correct_routes),
        ),
    )
    return 0


def main() -> int:
    """Congela alvos e ID; aguarda encerramento antes de acessar qualquer site."""
    global PROOF_BYTES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--targets", type=Path, default=ROOT / "data/coverage_priority_sites.csv")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--freeze-now",
        action="store_true",
        help="Congela capa/editorias agora e compara após a rodada terminar",
    )
    parser.add_argument(
        "--correct-gap-routes",
        action="store_true",
        help="Uma passagem limitada por links oficiais para lacunas comprovadas",
    )
    args = parser.parse_args()
    if args.correct_gap_routes and not args.freeze_now:
        parser.error("--correct-gap-routes exige --freeze-now")
    if not re.fullmatch("[a-f0-9]{32}", args.run_id):
        parser.error("ID de rodada inválido")
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "audit.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        PROOF_BYTES = sum(p.stat().st_size for p in (args.output / "html").glob("*.gz"))
        configure_environment()
        with args.targets.open() as f:
            targets = list(csv.DictReader(f))
        if len(targets) != 120 or len({r["source_id"] for r in targets}) != 120:
            parser.error("Informe exatamente 120 fontes distintas")
        manifest_path = args.output / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest["run_id"] != args.run_id:
                raise ValueError("Diretório pertence a outra rodada")
        else:
            ids = [int(t["source_id"]) for t in targets]
            sources = read_db(
                "SELECT id,name,url,active,deleted_at FROM midias_manager.sites WHERE id=ANY(%s)",
                (ids,),
            )
            profiles = read_db(
                "SELECT DISTINCT ON(source_id) source_id,access_transport "
                "FROM midias_manager.links WHERE source_id=ANY(%s) AND deleted_at IS NULL "
                "ORDER BY source_id,active DESC,id",
                (ids,),
            )
            by_id = {str(s["id"]): s for s in sources}
            transports = {str(p["source_id"]): p["access_transport"] for p in profiles}
            for target in targets:
                source = by_id[target["source_id"]]
                if source["deleted_at"]:
                    raise ValueError("Fonte alvo excluída")
                target.update(
                    source_id=int(target["source_id"]),
                    homepage=source["url"],
                    active=source["active"],
                    transport=transports.get(target["source_id"], "direct"),
                )
            manifest = dict(
                run_id=args.run_id,
                targets=targets,
                limits=LIMITS,
                created_at=datetime.now(UTC).isoformat(),
                writes_catalog=False,
                writes_corpus=False,
                bright_data=False,
                openrouter=False,
            )
            write(manifest_path, manifest)
        if args.freeze_now:
            return frozen_audit(manifest, args.output, correct_routes=args.correct_gap_routes)
        if "run" not in manifest:
            run = wait_for_run(args.run_id, args.output)
            polls = read_db(
                "SELECT (snapshot->'source'->>'id')::bigint source_id,count(*) n "
                "FROM midias_manager.ingest_polls WHERE run_id=%s GROUP BY 1",
                (args.run_id,),
            )
            counts = {p["source_id"]: p["n"] for p in polls}
            for target in manifest["targets"]:
                target["links_in_run"] = counts.get(target["source_id"], 0)
            manifest.update(run=run, comparison_at=datetime.now(UTC).isoformat())
            write(manifest_path, manifest)
            manifest = json.loads(manifest_path.read_text())
        write(
            args.output / "status.json",
            dict(state="auditing", run_id=args.run_id, started_at=datetime.now(UTC)),
        )
        results = []
        with ThreadPoolExecutor(max_workers=LIMITS["workers"]) as pool:
            futures = [
                pool.submit(audit_source, t, manifest, args.output) for t in manifest["targets"]
            ]
            for future in as_completed(futures):
                results.append(future.result())
                report(results, manifest, args.output)
                write(
                    args.output / "status.json",
                    dict(
                        state="auditing",
                        completed_sites=len(results),
                        total=120,
                        run_id=args.run_id,
                    ),
                )
        write(
            args.output / "status.json",
            dict(
                state="completed",
                completed_sites=120,
                run_id=args.run_id,
                finished_at=datetime.now(UTC),
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
