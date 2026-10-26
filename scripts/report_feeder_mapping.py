"""Consolida a rodada Feeder sem novas chamadas HTTP ou alterações no catálogo."""

# ruff: noqa: E501 -- Literais HTML do relatório local.
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import UTC, datetime
from html import escape
from pathlib import Path


def build_report(out: Path, *, evidence_prefix="feeder", description=None):
    """Publica links de procedência, notícias e evidências e mantém decisões explícitas."""
    results = [json.loads(p.read_text()) for p in (out / "sources").glob("*.json")]
    applied = [d for p in (out / "applied").glob("*.json") for d in json.loads(p.read_text())]
    decisions = Counter(d["action"] for d in applied)
    count = Counter(c["decision"] for r in results for c in r["candidates"])
    all_candidates = []
    rows = []
    for result in sorted(results, key=lambda r: r["source"]["id"]):
        source = result["source"]
        successes = [r for r in result["results"] if r["confirmed"]]
        actions = [d for d in applied if d.get("source_id") == source["id"] and d.get("link_id")]
        details = []
        for item in successes:
            candidate = item["candidate"]
            proof = next(e for e in item["evidence"] if e["status"] == "captured")
            status = next(
                (d["action"] for d in applied if d.get("url") == candidate["url"]),
                "Apenas comprovação técnica",
            )
            details.append(
                '<li><a target="_blank" rel="noopener noreferrer" href="'
                + escape(candidate["url"], quote=True)
                + '">'
                + escape(candidate["label"])
                + "</a> · "
                + escape(status)
                + "<br>"
                + '<a target="_blank" rel="noopener noreferrer" href="'
                + escape(proof["url"], quote=True)
                + '">Notícia</a> · '
                + '<a target="_blank" href="/evidencias/'
                + escape(evidence_prefix, quote=True)
                + "/"
                + proof["url_hash"]
                + '">Ver evidência</a> · '
                + escape(str(proof.get("published_at") or "Data não confirmada"))
                + "</li>"
            )
        local = Counter(c["decision"] for c in result["candidates"])
        counts = "; ".join(f"{key}: {value}" for key, value in local.items())
        rows.append(
            "<tr><td>"
            + escape(source["name"])
            + "</td><td>"
            + str(len(result["candidates"]))
            + "</td><td>"
            + str(len(result["results"]))
            + "</td><td>"
            + str(len(successes))
            + "</td><td>"
            + str(len(actions))
            + "</td><td><ul>"
            + "".join(details)
            + "</ul><details><summary>Classificação dos candidatos</summary>"
            + escape(counts)
            + "</details></td></tr>"
        )
        for c in result["candidates"]:
            all_candidates.append(
                {
                    k: c.get(k)
                    for k in (
                        "source_id",
                        "url",
                        "label",
                        "origin",
                        "decision",
                        "provenance",
                        "existing_id",
                        "feeder_status",
                    )
                }
            )
    with (out / "candidatos.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(all_candidates[0]))
        writer.writeheader()
        writer.writerows(all_candidates)
    page = """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Descoberta de RSS via Feeder</title>
    <style>body{font:16px system-ui;margin:24px;color:#183b35}table{border-collapse:collapse;width:100%}td,th{padding:12px;border:1px solid #ddd;text-align:left;vertical-align:top}a{color:#006c65}li{margin:12px 0}</style>
    <h1>Descoberta de RSS via Feeder</h1>"""
    page += f"<p>{len(results)} fontes consultadas · {len(all_candidates)} candidatos · {count['confirmed']} comprovados · {decisions['feeder_create']} criados · {decisions['feeder_restore']} reativados.</p>"
    page += """<p>O Feeder fornece candidatos; XML, notícia textual e evidência foram testados diretamente nos veículos.
    Até 8 candidatos por fonte, 3 perfis HTTP e 120 segundos por fonte. Os demais ficaram registrados como pendentes de orçamento, sem ativação.
    Feeds de terceiros, agregadores e podcasts foram separados. RSS funcional participa da busca diária; a data de cada notícia é filtrada na rodada, sem excluir o feed pela idade da amostra.
    Exclusões/inativações manuais e revisões posteriores foram preservadas. Não houve análise de contexto, uso de serviços pagos ou nova rodada de ingestão.</p>
    <p>Legenda: already_functional = já funcional no catálogo; confirmed = notícia e evidência comprovadas;
    not_confirmed = sem comprovação nesta amostra; third_party_review = origem externa para revisão;
    duplicate_other_source = já vinculado a outra fonte; budget_deferred = fora dos limites desta rodada;
    non_editorial = conteúdo fora do escopo. Criado/reativado descreve a aplicação no catálogo, não cobertura integral do veículo.</p>
    <table><thead><tr><th>Fonte</th><th>Candidatos</th><th>RSS testados</th><th>Comprovados</th><th>Aplicados</th><th>Notícias e evidências</th></tr></thead><tbody>"""
    if description:
        page = page.replace(
            "Até 8 candidatos por fonte, 3 perfis HTTP e 120 segundos por fonte. "
            "Os demais ficaram registrados como pendentes de orçamento, sem ativação.",
            escape(description),
        )
    (out / "report.html").write_text(page + "".join(rows) + "</tbody></table></html>")
    report = dict(
        generated_at=datetime.now(UTC).isoformat(),
        sources=len(results),
        candidates=len(all_candidates),
        tested=sum(len(r["results"]) for r in results),
        candidate_decisions=dict(count),
        application=dict(decisions),
    )
    (out / "report-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report
