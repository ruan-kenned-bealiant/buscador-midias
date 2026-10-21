"""Uma única correção dos checkpoints com erro interno, sem repetir fontes concluídas."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import audit_problem_sources as audit
import consolidate_round12 as reports
import validate_round12_samples as samples
from buscador_midias.discovery.xml_review import review_xml
from buscador_midias.sites.loader import load_sites


def repair_compressed(out: Path) -> list[str]:
    """Reinterpreta respostas compactadas válidas do cache, sem rede ou expansão."""
    marker = out / "correcoes-parser.json"
    if marker.exists():
        return cast(list[str], reports.read_json(marker)["urls"])
    repaired = []
    backup = out / "correcoes-parser-originais"
    backup.mkdir(exist_ok=True)
    for source in (out / "fontes").glob("*.json"):
        data = reports.read_json(source)
        changed = False
        for record in data["records"]:
            if record["xml_valido"] or record["http_status"] != 200:
                continue
            for url in dict.fromkeys([record["url"], record.get("final_url")]):
                if not url:
                    continue
                cache = out / "http-cache" / (hashlib.sha256(url.encode()).hexdigest() + ".gz")
                if not cache.exists():
                    continue
                with gzip.open(cache, "rb") as stream:
                    body = stream.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024 or not body.startswith((b"\x1f\x8b", b"PK")):
                    continue
                try:
                    xml = review_xml(body, url)
                except (ValueError, OverflowError):
                    continue
                if not changed:
                    shutil.copy2(source, backup / source.name)
                result_file = (
                    out / "resultados" / (samples.identity(data["key"], record["url"]) + ".json")
                )
                shutil.copy2(result_file, backup / result_file.name)
                record.update(
                    xml_valido=True,
                    tipo=xml.kind,
                    candidatas=len(xml.entries),
                    filhos_listados=len(xml.children),
                    detalhe_anterior=record["detalhe"],
                    detalhe="",
                    reprocessado_offline=True,
                )
                samples.save(result_file, record)
                repaired.append(record["url"])
                changed = True
                break
        if changed:
            data["functional"] = sum(
                r["xml_valido"] and r.get("uso_noticias", True) for r in data["records"]
            )
            samples.save(source, data)
    samples.save(out / "correcoes-parser.json", dict(urls=repaired, network_requests=0))
    return repaired


def main() -> None:
    """Corrige somente falhas técnicas registradas, preservando sua evidência original."""
    out = reports.ROOT / "var/auditoria-final-problemas"
    if reports.read_json(out / "execucao.json")["status"] != "completed":
        raise SystemExit("Aguarde a conclusão da revisão finita.")
    marker = out / "reparo-interno.json"
    if marker.exists():
        raise SystemExit("Correção interna já registrada; não haverá repetição automática.")
    plan = reports.read_json(out / "plano.json")
    pending = []
    for job in plan["jobs"]:
        path = out / "fontes" / (job["key"].replace("/", "__") + ".json")
        if reports.read_json(path).get("incomplete"):
            pending.append(job)
    samples.save(
        marker,
        dict(
            status="running",
            jobs=[j["key"] for j in pending],
            started_at=datetime.now(UTC).isoformat(),
            max_attempts=1,
        ),
    )
    audit.OUT = out
    samples.OUTPUT = out
    sites = {
        s.key: s
        for s in load_sites(reports.ROOT / "var/validacao-final/catalogo-congelado.csv").sites
    }
    saved = out / "erros-internos-originais"
    saved.mkdir(exist_ok=True)
    results = []
    for job in pending:
        path = out / "fontes" / (job["key"].replace("/", "__") + ".json")
        path.replace(saved / path.name)
        result = audit.safe_audit(job, sites[job["key"]])
        results.append(
            dict(
                fonte=job["key"],
                incomplete=result.get("incomplete", False),
                functional=result["functional"],
                texts=result["texts"],
            )
        )
    samples.save(
        marker,
        dict(
            status="completed",
            jobs=results,
            finished_at=datetime.now(UTC).isoformat(),
            max_attempts=1,
        ),
    )
    repaired = repair_compressed(out)
    print("Sitemaps compactados recuperados offline:", len(repaired))
    # Reconsolida os checkpoints; nenhuma fonte já salva é buscada novamente.
    subprocess.run(
        [
            sys.executable,
            str(reports.ROOT / "scripts/audit_problem_sources.py"),
            "--all-problems",
        ],
        check=True,
        cwd=reports.ROOT,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
