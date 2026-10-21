"""Confere escopo e páginas de áudio nas amostras salvas; nenhuma chamada de rede."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import consolidate_round12 as reports
import validate_round12_samples as samples
from buscador_midias.extraction.sample_audit import is_audio_destination
from buscador_midias.sites.loader import load_sites


def main() -> None:
    """Preserva a avaliação original e remove provas textuais de escopo incorreto ou áudio."""
    out = reports.ROOT / "var/auditoria-final-problemas"
    marker = out / "validacao-editorial.json"
    if marker.exists():
        raise SystemExit("Validação editorial já concluída.")
    sites = {
        s.key: s
        for s in load_sites(reports.ROOT / "var/validacao-final/catalogo-congelado.csv").sites
    }
    backup = out / "validacao-editorial-originais"
    backup.mkdir(exist_ok=True)
    changes = []
    for file in (out / "fontes").glob("*.json"):
        source = reports.read_json(file)
        changed = False
        for record in source["records"]:
            record_changed = False
            for sample in record["amostras"]:
                if not sample["texto_util_confirmado"]:
                    continue
                reason = (
                    "pagina_de_audio_dedicada"
                    if is_audio_destination(sample["url"])
                    else "fora_do_escopo_editorial"
                    if not sites[source["key"]].owns(sample["url"])
                    else ""
                )
                if not reason:
                    continue
                if not changed:
                    shutil.copy2(file, backup / file.name)
                path = (
                    out / "resultados" / (samples.identity(source["key"], record["url"]) + ".json")
                )
                if not record_changed:
                    shutil.copy2(path, backup / path.name)
                sample.update(
                    texto_util_confirmado_anterior=True,
                    texto_util_confirmado=False,
                    revisao_editorial=reason,
                )
                if reason == "pagina_de_audio_dedicada":
                    sample.update(audio_dedicado=True, categoria="audio_podcast")
                changes.append(
                    dict(
                        fonte=source["key"],
                        mecanismo=record["url"],
                        url=sample["url"],
                        motivo=reason,
                    )
                )
                changed = record_changed = True
            if record_changed:
                samples.save(path, record)
        if changed:
            source["texts"] = sum(
                s["texto_util_confirmado"] for r in source["records"] for s in r["amostras"]
            )
            samples.save(file, source)
    samples.save(marker, dict(network_requests=0, changes=changes))
    subprocess.run(
        [sys.executable, str(reports.ROOT / "scripts/audit_problem_sources.py"), "--all-problems"],
        check=True,
        cwd=reports.ROOT,
    )
    print(json.dumps(dict(corrections=len(changes), network_requests=0)))


if __name__ == "__main__":
    main()
