"""Corrige arquivos de cache trocados por concorrência; não permite nenhuma chamada de rede."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys

import audit_problem_sources as audit
import consolidate_round12 as reports
import validate_round12_samples as samples
from buscador_midias.net.fetcher import FetchResult
from buscador_midias.sites.loader import load_sites


class OfflineReader(audit.BudgetReader):
    """Nega requisições quando o cache não permite reconstruir a resposta."""

    def raw(self, url: str) -> FetchResult:
        """Lê somente cache persistente, sem acionar transporte para cache ausente."""
        path = audit.OUT / "http-cache" / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if not path.exists():
            return FetchResult(url, None, b"", "cached_response_unavailable")
        try:
            return super().raw(url)
        except (ValueError, UnicodeError, OSError, EOFError):
            return FetchResult(url, None, b"", "cached_response_unavailable")


def main() -> None:
    """Recupera metadados provados e preserva bytes irrecuperáveis para diagnóstico."""
    out = reports.ROOT / "var/auditoria-final-problemas"
    marker = out / "reparo-cache-offline.json"
    if marker.exists():
        raise SystemExit("Correção offline já registrada.")
    backup = out / "cache-originais-com-inconsistencia"
    backup.mkdir(exist_ok=True)
    by_hash = {}
    for file in (out / "resultados").glob("*.json"):
        row = reports.read_json(file)
        by_hash[hashlib.sha256(row["url"].encode()).hexdigest()] = row
    repaired = []
    for file in (out / "http-cache").glob("*.json"):
        body_file = file.with_suffix(".gz")
        data = file.read_bytes()
        if data.startswith(b"\x1f\x8b"):
            proof = by_hash.get(file.stem)
            if proof and proof["http_status"] == 200 and proof["xml_valido"]:
                shutil.copy2(file, backup / file.name)
                samples.save(
                    file,
                    dict(
                        url=proof["url"],
                        status=200,
                        error=None,
                        final_url=proof.get("final_url") or proof["url"],
                        content_type="application/xml",
                        has_body=body_file.exists(),
                    ),
                )
                repaired.append(
                    dict(url=proof["url"], action="metadados reconstruídos de prova HTTP/XML salva")
                )
        if body_file.exists() and body_file.open("rb").read(2) != b"\x1f\x8b":
            shutil.copy2(file, backup / file.name)
            body_file.replace(backup / body_file.name)
            file.unlink()
            repaired.append(
                dict(
                    cache=file.name,
                    action="corpo inválido preservado; resposta indisponível offline",
                )
            )
    audit.OUT = out
    samples.OUTPUT = out
    vars(audit)["BudgetReader"] = OfflineReader
    plan = reports.read_json(out / "plano.json")
    sites = {
        s.key: s
        for s in load_sites(reports.ROOT / "var/validacao-final/catalogo-congelado.csv").sites
    }
    restored = []
    for job in plan["jobs"]:
        path = out / "fontes" / (job["key"].replace("/", "__") + ".json")
        if not reports.read_json(path).get("incomplete"):
            continue
        path.replace(backup / path.name)
        result = audit.safe_audit(job, sites[job["key"]])
        restored.append(
            dict(
                fonte=job["key"],
                incomplete=result.get("incomplete", False),
                functional=result["functional"],
                texts=result["texts"],
            )
        )
    samples.save(marker, dict(network_requests=0, cache_repairs=repaired, sources=restored))
    subprocess.run(
        [sys.executable, str(reports.ROOT / "scripts/audit_problem_sources.py"), "--all-problems"],
        check=True,
        cwd=reports.ROOT,
    )
    print(json.dumps(restored, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
