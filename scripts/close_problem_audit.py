"""Consolida a revisão finita e verifica o histórico, sem novas requisições."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import consolidate_round12 as reports

ROOT = reports.ROOT
OUT = ROOT / "var/auditoria-final-problemas"


def main() -> None:
    """Publica diagnóstico por fonte e resumo dos quatro passos autorizados."""
    plan = reports.read_json(OUT / "plano.json")
    execution = reports.read_json(OUT / "execucao.json")
    if execution["status"] != "completed":
        raise SystemExit("Auditoria ainda não concluiu todos os grupos.")
    results = [
        reports.read_json(OUT / "fontes" / (j["key"].replace("/", "__") + ".json"))
        for j in plan["jobs"]
    ]
    snapshot = reports.read_json(ROOT / "var/consulta-funcionais/retrato.json")
    # O retrato deve ter sido exportado depois da conclusão da auditoria.
    if snapshot["meta"]["generated_at"] < execution["finished_at"]:
        raise SystemExit("Regenere o retrato funcional antes de encerrar.")
    before = reports.read_json(OUT / "painel-anterior/retrato.json")
    recovered = reports.read_json(OUT / "contagens-recuperadas.json")
    history = reports.read_json(OUT / "painel-anterior/historico-preservado.json")
    for name, digest in history["sha256"].items():
        with (ROOT / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("Arquivo histórico alterado: " + name)
    catalog = {s["key"]: s for s in snapshot["sources"]}
    rows: list[dict[str, Any]] = []
    details = []
    for job, result in zip(plan["jobs"], results, strict=True):
        reasons = Counter(
            r["detalhe"] or f"HTTP {r['http_status']}"
            for r in result["records"]
            if not r["xml_valido"]
        )
        current = catalog[job["key"]]
        status = (
            "erro_interno_documentado"
            if result.get("incomplete")
            else "texto_confirmado_na_amostra"
            if result["texts"]
            else "xml_sem_texto_confirmado_na_amostra"
            if result["functional"]
            else "sem_xml_confirmado_nesta_revisao"
        )
        row = dict(
            fonte=job["key"],
            nome=result["name"],
            grupo=job["group"],
            resultado=status,
            links_examinados=result["xml_tested"],
            xml_funcionais_na_revisao=result["functional"],
            textos_confirmados_na_revisao=result["texts"],
            links_funcionais_no_retrato=current["functional"],
            links_com_texto_no_retrato=current["text_mechanisms"],
            impedimentos=json.dumps(reasons, ensure_ascii=False),
            pendencias_na_lista_limitada=result["remaining_not_executed"],
            erro_interno=result["home_error"] if result.get("incomplete") else "",
        )
        rows.append(row)
        for record in result["records"]:
            details.append(
                dict(
                    fonte=job["key"],
                    grupo=job["group"],
                    url=record["url"],
                    xml_valido=record["xml_valido"],
                    tipo=record["tipo"],
                    http_status=record["http_status"],
                    impedimento=record["detalhe"],
                    urls_listadas=record["candidatas"] if record["xml_valido"] else None,
                    sitemaps_filhos=record["filhos_listados"] if record["xml_valido"] else None,
                    textos=sum(s["texto_util_confirmado"] for s in record["amostras"]),
                    observado_em=record["observado_em"],
                )
            )
    reports.OUTPUT = OUT
    reports.write_csv("diagnostico-fontes.csv", rows)
    reports.write_csv("diagnostico-links.csv", details)
    reports.workbook({"Diagnóstico por fonte": rows, "Diagnóstico por link": details})
    before_keys = {s["key"] for s in before["sources"] if s["functional"]}
    after_keys = {s["key"] for s in snapshot["sources"] if s["functional"]}
    summary = dict(
        completed_at=datetime.now(UTC).isoformat(),
        sources=len(rows),
        groups=dict(Counter(j["group"] for j in plan["jobs"])),
        sources_recovered=sum(r["before"] == 0 and r["functional"] > 0 for r in results),
        newly_functional=sorted(after_keys - before_keys),
        current_confirmation_lost=sorted(before_keys - after_keys),
        sources_with_text_in_review=sum(r["texts"] > 0 for r in results),
        internal_errors=sum(bool(r.get("incomplete")) for r in results),
        counts_recovered_offline=recovered["recovered"],
        before_functional_sources=before["meta"]["sources_functional"],
        after_functional_sources=snapshot["meta"]["sources_functional"],
        sources_with_text=snapshot["meta"]["sources_with_text"],
        text_mechanisms=snapshot["meta"]["text_mechanisms"],
        missing_counts=snapshot["meta"]["unknown_listed"],
        missing_round_attribution=snapshot["meta"]["unknown_round"],
        original_files_verified=len(history["sha256"]),
        automatic_collection=False,
        paid_services=False,
    )
    (OUT / "conclusao.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    text = [
        "# Revisão final das fontes problemáticas",
        "",
        f"Lista congelada: {len(rows)} fontes, sem repetir os 20 casos dos dois lotes anteriores.",
        "Máximo por fonte: 12 mecanismos, quatro artigos, 60 requisições e 180 segundos. "
        "Profundidade dois; sem filtro de publicação. Nenhuma expansão para outras fontes.",
        "",
        "## Resultado dos quatro passos",
        "",
        f"1. **Sem XML comprovado:** {summary['groups'][1]} fontes revisadas; "
        f"{summary['sources_recovered']} tiveram XML funcional confirmado.",
        f"2. **Sem acesso textual comprovado ou com bloqueio em amostras:** "
        f"{summary['groups'][2]} fontes revisadas.",
        f"3. **Outros mecanismos inconclusivos:** {summary['groups'][3]} fontes revisadas, "
        "com testes limitados dos endereços e declarações oficiais. Não significa que todos "
        "os mecanismos inconclusivos do inventário foram retestados.",
        f"4. **Não registrado:** {recovered['recovered']} contagens recuperadas exclusivamente "
        "de XMLs históricos salvos. Amostras novas não alteram "
        "a procedência histórica da rodada 12.",
        "",
        f"Texto confirmado nesta revisão: {summary['sources_with_text_in_review']} fontes. "
        f"Retrato atualizado: {summary['after_functional_sources']} fontes com XML funcional "
        f"(antes: {summary['before_functional_sources']}) e {summary['sources_with_text']} fontes "
        f"com evidência textual histórica ou desta revisão. "
        f"Há {summary['text_mechanisms']} links com texto comprovado em amostras, excluindo "
        "canais de áudio comprovados do CSV de seleção textual.",
        "",
        "As contagens de XML funcional e texto acessível têm critérios diferentes. Uma falha "
        "recente não apaga evidência histórica, mas prevalece no retrato atual daquele endpoint. "
        "Limites próprios de tempo/tamanho conservam a prova histórica e ficam sinalizados. "
        "Endpoints não retestados conservam sua evidência anterior. Por isso o total pode "
        "crescer em algumas fontes e diminuir em outras. Não é uma medida de cobertura integral.",
        "",
        "## Fontes recuperadas",
        "",
        "| Fonte | XML confirmados nesta revisão | Textos em amostras |",
        "|---|---:|---:|",
    ]
    text += [
        f"| {r['name']} ({r['key']}) | {r['functional']} | {r['texts']} |"
        for r in results
        if r["before"] == 0 and r["functional"]
    ]
    text += [
        "",
        "## Falhas atuais em fontes antes confirmadas",
        "",
        "As provas anteriores permanecem no histórico. Nesta revisão não foi possível "
        "reconfirmar os endereços conhecidos destas fontes:",
    ]
    text += [
        "- " + catalog[key]["name"] + " (" + key + ")"
        for key in summary["current_confirmation_lost"]
    ]
    text += [
        "",
        "## Pendências e preservação",
        "",
        f"Ainda existem {summary['missing_counts']} mecanismos funcionais com contagem "
        f"sem data desconhecida e {summary['missing_round_attribution']} com atribuição "
        "individual da rodada 12 desconhecida. Esses campos não são convertidos em zero. "
        "Quando falta o XML original ou a procedência individual, uma nova consulta não "
        "reconstitui com segurança o que ocorreu na coleta histórica.",
        "",
        f"Falhas internas explicitamente registradas: {summary['internal_errors']}. "
        "As pendências e os motivos por fonte/link estão em diagnostico-fontes.csv, "
        "diagnostico-links.csv e consolidacao.xlsx. Bloqueios, robots, timeout, XML vazio "
        "ou HTML não são prova de inexistência de RSS/sitemap.",
        "",
        f"Integridade conferida por SHA-256 em {summary['original_files_verified']} arquivos "
        "originais. A rodada 12, filas, caches, notícias, inventários e etapas anteriores "
        "foram mantidos. O retrato anterior está em painel-anterior/. Nenhuma coleta contínua, "
        "agendamento ou serviço pago foi ativado. Rodada encerrada, sem repetição automática.",
    ]
    (OUT / "relatorio.md").write_text("\n".join(text) + "\n")
    delivery = ROOT / "var/consulta-funcionais"
    for name in ("diagnostico-fontes.csv", "diagnostico-links.csv", "conclusao.json"):
        shutil.copy2(OUT / name, delivery / name)
    shutil.copy2(OUT / "consolidacao.xlsx", delivery / "auditoria-final.xlsx")
    audit_reports = [
        ROOT / "var/auditoria-dirigida-10/relatorio.md",
        ROOT / "var/auditoria-dirigida-10-02/relatorio.md",
        OUT / "relatorio.md",
    ]
    (delivery / "auditoria-dirigida.md").write_text(
        "\n\n---\n\n".join(p.read_text() for p in audit_reports), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
