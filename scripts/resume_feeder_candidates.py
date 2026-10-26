"""Conclui exclusivamente os candidatos Feeder pendentes, sem repetir os testes anteriores."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from psycopg.rows import dict_row

import map_feeder_candidates as mapping
from buscador_midias.catalog.postgres import connect_postgres
from map_editorial_priority import credentials, write
from report_feeder_mapping import build_report

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = ROOT / "var/gerenciador/mapeamento-feeder-20261026"
OUT = ROOT / "var/gerenciador/feeder-pendentes-20261026"
LOCK = threading.Lock()
HOST_LOCKS = {}


def freeze():
    """Congela somente pendências conhecidas e as revisões atuais do catálogo."""
    candidates = defaultdict(list)
    for path in (PREVIOUS / "sources").glob("*.json"):
        row = json.loads(path.read_text())
        for candidate in row["candidates"]:
            if candidate["decision"] == "budget_deferred":
                candidates[row["source"]["id"]].append(candidate)
    ids = list(candidates)
    with connect_postgres(credentials()) as db:
        db.row_factory = dict_row
        db.execute("SET TRANSACTION READ ONLY")
        sources = db.execute(
            "SELECT * FROM midias_manager.sites WHERE id=ANY(%s) ORDER BY id", (ids,)
        ).fetchall()
        # A auditoria completa anterior permanece preservada; aqui basta a revisão para deduplicar.
        links = db.execute(
            "SELECT id,source_id,url,version,active,deleted_at,model,access_transport,"
            "jsonb_build_object('usable_for_news',coalesce((last_test->>'usable_for_news')::boolean,false)) last_test "
            "FROM midias_manager.links"
        ).fetchall()
        models = db.execute("SELECT * FROM midias_manager.mechanisms").fetchall()
    largest = max(map(len, candidates.values()))
    manifest = dict(
        id="feeder-pendentes-20261026",
        started_at=datetime.now(UTC).isoformat(),
        sources=sources,
        links=links,
        models=models,
        candidates=dict(candidates),
        expected_candidates=sum(map(len, candidates.values())),
        probe_limits=dict(
            max_candidates=largest,
            source_seconds=largest * 3 * 20 + 30,
            test_seconds=20,
            storage_bytes=160 * 1024 * 1024,
            immutable_evidence=True,
        ),
        workers=8,
        max_profiles=3,
        bright_data=False,
        openrouter=False,
        corpus_writes=False,
    )
    with gzip.open(OUT / "manifest.json.gz", "wt") as handle:
        json.dump(manifest, handle, ensure_ascii=False, default=str)
    return manifest


def process(source, manifest):
    """Serializa as fontes do mesmo host; todos os candidatos congelados recebem uma decisão."""
    host = (urlsplit(source["url"]).hostname or "").removeprefix("www.")
    with LOCK:
        lock = HOST_LOCKS.setdefault(host, threading.Lock())
    candidates = manifest["candidates"][str(source["id"])]
    with lock:
        return mapping.probe(
            source, dict(candidates=candidates, provenance=candidates[0]["provenance"]), manifest
        )


def main():
    """Executa lote finito e retomável; aplicação depende de evidência validada e revisão atual."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    mapping.OUT = OUT
    mapping.RUN = "feeder-pendentes-20261026"
    path = OUT / "manifest.json.gz"
    if path.exists():
        with gzip.open(path, "rt") as handle:
            manifest = json.load(handle)
    else:
        manifest = freeze()
        # Uniformiza o formato das chaves JSON na primeira execução e nas retomadas.
        manifest["candidates"] = {str(k): v for k, v in manifest["candidates"].items()}
    print(
        "PLAN",
        manifest["expected_candidates"],
        "candidatos de",
        len(manifest["sources"]),
        "fontes",
        flush=True,
    )
    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(process, source, manifest) for source in manifest["sources"]]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(
                json.dumps(
                    dict(
                        done_sources=len(rows),
                        source=row["source"]["name"],
                        candidates=len(row["candidates"]),
                        tested=len(row["results"]),
                        confirmed=sum(r["confirmed"] for r in row["results"]),
                    ),
                    ensure_ascii=False,
                ),
                flush=True,
            )
    applied = []
    if args.apply:
        for row in sorted(rows, key=lambda r: r["source"]["id"]):
            path = OUT / "applied" / f"{row['source']['id']}.json"
            decisions = (
                json.loads(path.read_text())
                if path.exists()
                else mapping.apply_source(row, manifest)
            )
            write(path, decisions)
            applied.extend(decisions)
    proofs = [
        e
        for row in rows
        for item in row["results"]
        for e in item["evidence"]
        if e["status"] == "captured"
    ]
    for proof in proofs:
        assert (
            hashlib.sha256((OUT / "evidence" / proof["path"]).read_bytes()).hexdigest()
            == proof["sha256"]
        )
    summary = dict(
        started_at=manifest["started_at"],
        finished_at=datetime.now(UTC).isoformat(),
        expected_candidates=manifest["expected_candidates"],
        sources=len(rows),
        tested=sum(len(r["results"]) for r in rows),
        decisions=dict(Counter(c["decision"] for row in rows for c in row["candidates"])),
        confirmed=sum(item["confirmed"] for row in rows for item in row["results"]),
        proofs_verified=len(proofs),
        application=dict(Counter(d["action"] for d in applied)),
    )
    write(OUT / "summary.json", summary)
    write(OUT / "application.json", applied)
    build_report(
        OUT,
        evidence_prefix="feeder-pendentes",
        description="Conclusão dos 281 candidatos pendentes da rodada anterior. Até 3 perfis HTTP e 20 segundos por teste; sem limite de 8 candidatos por fonte nesta rodada.",
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
