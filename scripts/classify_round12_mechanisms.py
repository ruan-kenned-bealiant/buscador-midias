"""Etapa 4: classificação local e reversível, separada do inventário e da execução."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lxml import etree

import consolidate_round12 as reports
from buscador_midias.analysis.mechanism_selection import (
    HISTORICAL_DOCUMENT_THRESHOLD,
    MechanismEvidence,
    classify,
)
from buscador_midias.domain.url import canonicalize_url
from buscador_midias.sites.loader import load_sites
from buscador_midias.strategy.manual import is_excluded, load_manual_rules
from buscador_midias.strategy.mechanism_inventory import safe_mechanism_url

ROOT = reports.ROOT
ROUND = reports.ROUND
BASE = ROOT / "var/analise-rodada-12"
OUTPUT = BASE / "etapa-4"
LABELS = {
    "frequente": "Busca frequente",
    "periodico": "Verificação periódica",
    "excluir": "Excluir da seleção textual",
    "inconclusivo": "Inconclusivo",
}
REASONS = {
    "texto_confirmado_por_url_observada":
        "Há uma página com texto útil vinculada a este mecanismo.",
    "xml_interpretado_sem_amostra_textual_na_janela":
        "XML interpretado, sem amostra textual nesta janela; preservar para verificação periódica.",
    "amostra_sem_texto_util_confirmado":
        "As amostras não confirmaram texto útil; não basta para excluir o mecanismo.",
    "evidencia_individual_insuficiente_ou_falha_de_acesso":
        "Falta comprovação individual ou houve falha/bloqueio; manter inconclusivo.",
    "origem_sem_url_http_reutilizavel_ou_fora_do_escopo":
        "O registro não é uma URL HTTP utilizável da fonte; não usar como endpoint.",
    "origem_http_sem_tipo_xml_comprovado_individualmente":
        "Há endereço de origem, mas seu tipo RSS/sitemap não foi comprovado individualmente.",
    "candidatas_cobertas_por_opcoes_frequentes":
        "Outras opções frequentes cobriram as mesmas candidatas neste retrato; "
        "preservar complemento.",
    "texto_confirmado_trabalho_documental_alto":
        "Há texto útil, mas o percurso exigiu muitos documentos; "
        "manter como complemento periódico.",
    "texto_confirmado_percurso_parcial_preservado":
        "Há texto útil e documentos pendentes; preservar a limitação da leitura parcial.",
    "audio_dedicado_comprovado_no_mecanismo_e_na_amostra":
        "Canal de podcast comprovado no XML local e nas amostras, sem texto útil confirmado.",
    "audio_na_amostra_sem_prova_de_endpoint_exclusivo":
        "Amostra de áudio sem prova suficiente de canal exclusivamente de áudio.",
    "exclusao_manual_preexistente": "O mecanismo está coberto por exclusão manual já registrada.",
    "preservar_opcao_textual_da_fonte_custo_ou_percurso_com_ressalvas":
        "Preservar uma opção textual da fonte, com ressalva de custo ou percurso incompleto.",
}


def rows(path: Path) -> list[dict[str, str]]:
    """Lê relatórios anteriores."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def key(site: str, url: str) -> tuple[str, str]:
    """Chave estável compatível com as etapas anteriores."""
    return site, canonicalize_url(url)


def podcast_proof(url: str) -> dict[str, Any]:
    """Examina somente XMLs locais conhecidos, sem expandir ou consultar o endpoint."""
    digest = hashlib.sha256(url.encode()).hexdigest()
    for directory in [ROUND / "cache-http-fila", ROOT / "var/validacao-final/cache"]:
        file = directory / (digest + ".gz")
        if not file.exists():
            continue
        with gzip.open(file, "rb") as stream:
            payload = stream.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            continue
        try:
            tree: Any = etree.fromstring(
                payload, etree.XMLParser(resolve_entities=False, no_network=True)
            )
        except (ValueError, etree.XMLSyntaxError):
            continue
        if etree.QName(tree).localname.lower() not in ("rss", "feed", "rdf"):
            continue
        namespace = any(
            "podcast" in value.lower() or "itunes" in value.lower() for value in tree.nsmap.values()
        )
        mime_types = tree.xpath(
            '//*[local-name()="enclosure"]/@type | '
            '//*[local-name()="link" and @rel="enclosure"]/@type'
        )
        audio = sum(str(mime).lower().startswith("audio/") for mime in mime_types)
        titles = tree.xpath(
            '/*/*[local-name()="title"]/text() | '
            '/*/*[local-name()="channel"]/*[local-name()="title"]/text()'
        )
        if namespace and audio and audio == len(mime_types):
            return dict(
                comprovado=True,
                arquivo=str(file.relative_to(ROOT)),
                titulo=" | ".join(str(t) for t in titles),
                enclosures_audio=audio,
                namespace_podcast=True,
                ressalva="Evidência local de canal podcast, combinada com amostras da etapa 2",
            )
    return dict(
        comprovado=False,
        arquivo="",
        titulo="",
        enclosures_audio=0,
        namespace_podcast=False,
        ressalva="Sem XML local suficiente para provar canal dedicado",
    )


def main() -> None:
    """Classifica mecanismos e preserva opções textuais utilizáveis por fonte."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    first = {
        key(r["fonte"], r["url"]): r
        for r in rows(BASE / "etapa-1/mecanismos.csv")
        if r["tipo"] != "bootstrap"
    }
    second = {key(r["fonte"], r["mecanismo"]): r for r in rows(BASE / "etapa-2/mecanismos.csv")}
    third = rows(BASE / "etapa-3/mecanismos.csv")
    sources = {r["fonte"]: r for r in rows(BASE / "etapa-1/fontes.csv")}
    sites = {
        s.key: s for s in load_sites(ROOT / "var/validacao-final/catalogo-congelado.csv").sites
    }
    rules = load_manual_rules(ROOT / "var/validacao-final/regras-congeladas.csv")
    closure = reports.read_json(ROUND / "encerramento.json")
    candidate_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    observed_types: dict[tuple[str, str], str] = {}
    with sqlite3.connect(f"file:{ROUND / 'fila-documentos.sqlite'}?mode=ro", uri=True) as queue:
        root_keys = {r[0]: key(r[1], r[2]) for r in queue.execute("SELECT id,site,url FROM roots")}
        for root_id, url_hash in queue.execute("SELECT root_id,hash FROM evidence"):
            candidate_sets[root_keys[root_id]].add(url_hash)
        for site, url, kind in queue.execute("""SELECT site,url,
                CASE WHEN json_extract(outcome,'$.format') IN ('rss','atom','rdf')
                     THEN 'feed' ELSE 'sitemap' END
                FROM tasks WHERE state='done' AND json_extract(outcome,'$.valid')>0"""):
            observed_types[key(site, url)] = kind

    classifications: list[dict[str, Any]] = []
    audio_reviews = []
    for metrics in third:
        site, url = metrics["fonte"], metrics["mecanismo"]
        identity = key(site, url)
        original = first.get(identity, {})
        sample = second[identity]
        texts = max(int(metrics["textos_confirmados_entre_candidatas"]), int(sample["com_texto"]))
        audio = int(sample["audio_podcast"])
        proof = podcast_proof(url) if audio else dict(comprovado=False)
        if audio:
            audio_reviews.append(
                dict(
                    fonte=site,
                    mecanismo=url,
                    amostras_audio=audio,
                    textos_confirmados=texts,
                    exemplo_audio=sample["exemplo_audio"],
                    **proof,
                )
            )
        http = url.startswith(("http://", "https://")) and safe_mechanism_url(sites[site], url)
        kind = metrics["tipo"]
        if kind == "origem_direta":
            kind = observed_types.get(identity, "tipo_xml_nao_confirmado")
        reusable = http and kind in ("feed", "sitemap", "news_sitemap")
        manual = is_excluded(url, rules.for_site(sites[site]).exclude)
        documents = (
            int(metrics["documentos_processados"]) if metrics["documentos_processados"] else None
        )
        pending = int(metrics["documentos_pendentes"] or 0)
        xml_confirmed = bool(int(original.get("xmls_validos", 0))) or identity in observed_types
        evidence = MechanismEvidence(
            reusable,
            manual,
            xml_confirmed,
            texts,
            int(sample["amostras_verificadas"]),
            audio,
            proof["comprovado"],
            documents,
            pending,
        )
        decision, reason = classify(evidence)
        if http and not reusable:
            reason = "origem_http_sem_tipo_xml_comprovado_individualmente"
        classifications.append(
            dict(
                fonte=site,
                nome=sources[site]["nome"],
                mecanismo=url,
                tipo_original=metrics["tipo"],
                tipo_reutilizavel=kind,
                classificacao=decision,
                motivo=reason,
                http_no_escopo=http,
                endpoint_reutilizavel=reusable,
                xml_confirmado=xml_confirmed,
                textos_confirmados=texts,
                amostras_verificadas=int(sample["amostras_verificadas"]),
                audio_na_amostra=audio,
                canal_podcast_comprovado=proof["comprovado"],
                candidatas_observadas=int(metrics["candidatas_observadas"]),
                exclusivas_entre_raizes=metrics["exclusivas_entre_raizes"],
                documentos_processados=documents,
                documentos_pendentes=pending,
                custo_desconhecido=documents is None,
                tipo_evidencia=metrics["evidencia_conjunto"],
                alternativa_frequente="",
                exemplo_textual=metrics["exemplo_textual"] or sample["exemplo_textual"],
                exemplo_audio=sample["exemplo_audio"],
                fonte_parcial=site in closure["incomplete_sources"],
                regra_documentos_periodicos=HISTORICAL_DOCUMENT_THRESHOLD,
                limite="Classificação da evidência observada; "
                "sem garantia de cobertura ou atualização futura",
            )
        )

    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in classifications:
        by_source[row["fonte"]].append(row)
    for site, members in by_source.items():
        # RSS/editorias preservam prioridade mesmo quando coincidiram nesta janela.
        ordered = sorted(
            members,
            key=lambda r: (
                {"feed": 0, "news_sitemap": 1, "sitemap": 2}.get(r["tipo_reutilizavel"], 3),
                r["custo_desconhecido"],
                r["documentos_processados"] or 0,
                r["mecanismo"],
            ),
        )
        kept: list[dict[str, Any]] = []
        covered: set[str] = set()
        for row in ordered:
            if row["classificacao"] != "frequente":
                continue
            values = candidate_sets.get(key(site, row["mecanismo"]), set())
            if row["tipo_reutilizavel"] == "sitemap" and values and values <= covered:
                row.update(
                    classificacao="periodico",
                    motivo="candidatas_cobertas_por_opcoes_frequentes",
                    alternativa_frequente=" | ".join(r["mecanismo"] for r in kept)[:3000],
                )
                continue
            kept.append(row)
            covered.update(values)
        eligible = [
            r
            for r in ordered
            if r["textos_confirmados"]
            and r["endpoint_reutilizavel"]
            and r["classificacao"] != "excluir"
        ]
        if eligible and not kept:
            best = min(
                eligible,
                key=lambda r: (
                    r["documentos_pendentes"] > 0,
                    r["custo_desconhecido"],
                    r["documentos_processados"] or 0,
                    r["mecanismo"],
                ),
            )
            best.update(
                classificacao="frequente",
                motivo="preservar_opcao_textual_da_fonte_custo_ou_percurso_com_ressalvas",
            )

    for row in classifications:
        row['uso_proposto'] = LABELS[row['classificacao']]
        row['explicacao'] = REASONS[row['motivo']]

    source_rows: list[dict[str, Any]] = []
    for site, source in sources.items():
        members = by_source[site]
        counts = Counter(r["classificacao"] for r in members)
        source_rows.append(
            dict(
                fonte=site,
                nome=source["nome"],
                categoria_coleta=source["categoria"],
                frequentes=counts["frequente"],
                periodicos=counts["periodico"],
                excluidos=counts["excluir"],
                inconclusivos=counts["inconclusivo"],
                possui_texto_confirmado=any(r["textos_confirmados"] for r in members),
                possui_endpoint_frequente=bool(counts["frequente"]),
                exemplo_endpoint_frequente=next(
                    (r["mecanismo"] for r in members if r["classificacao"] == "frequente"), ""
                ),
                lacuna_proveniencia=any(r["textos_confirmados"] for r in members)
                and not counts["frequente"],
                fonte_parcial=site in closure["incomplete_sources"],
            )
        )
    counts = Counter(r["classificacao"] for r in classifications)
    assert sum(counts.values()) == len(classifications) == 40502
    assert len(source_rows) == 1294
    assert all(
        r["endpoint_reutilizavel"] and r["textos_confirmados"]
        for r in classifications
        if r["classificacao"] == "frequente"
    )
    assert not any(
        r["textos_confirmados"]
        for r in classifications
        if r["classificacao"] == "excluir" and r["motivo"] != "exclusao_manual_preexistente"
    )
    assert all(
        not r["lacuna_proveniencia"] or not r["possui_endpoint_frequente"] for r in source_rows
    )
    criteria = [
        "Classificação offline e reversível; "
        "inventário, banco e planos de coleta não são alterados.",
        "Frequente requer URL HTTP própria, tipo RSS/sitemap utilizável "
        "e texto confirmado por URL.",
        "Texto em mecanismo misto preserva sua elegibilidade; "
        "áudio isolado não prova canal dedicado.",
        "Exclusão por áudio requer XML local de podcast com enclosures de áudio e amostra de áudio "
        "sem texto confirmado. Exclusões manuais preexistentes também são respeitadas.",
        "Sem novidades não é erro nem exclusão: XML interpretado sem amostra entra em periódico.",
        "Bloqueio, falha, falta de prova individual e rótulos sintéticos permanecem inconclusivos.",
        f"Acima de {HISTORICAL_DOCUMENT_THRESHOLD} documentos observados, preferir periódico. "
        "Esse limiar é uma política de classificação, não limite de execução ou tempo medido.",
        "Sitemap com candidatas integralmente cobertas por opções frequentes anteriores "
        "fica periódico. "
        "RSS/editorias não são rebaixados apenas por coincidirem nesta janela.",
        "Quando só há opção textual cara/parcial, preservar uma frequente com ressalva explícita. "
        "Isso preserva fontes, sem prometer que a próxima execução seja rápida ou completa.",
        "Custo ausente não vira zero. Origens de primeira gravação não comprovam exclusividade.",
        "Periodicidade e execução comparativa serão definidas na etapa seguinte; "
        "nenhum timer foi criado.",
    ]
    summary: dict[str, Any] = dict(
        rodada=12,
        etapa=4,
        gerado_em=datetime.now(UTC).isoformat(),
        fontes=1294,
        mecanismos_e_origens=len(classifications),
        por_classificacao=dict(counts),
        fontes_com_endpoint_frequente=sum(r["possui_endpoint_frequente"] for r in source_rows),
        fontes_com_texto_mas_sem_endpoint_frequente=sum(
            r["lacuna_proveniencia"] for r in source_rows
        ),
        origens_nao_reutilizaveis=sum(not r["endpoint_reutilizavel"] for r in classifications),
        motivos=dict(Counter(r["motivo"] for r in classifications)),
        limite_documentos_classificacao=HISTORICAL_DOCUMENT_THRESHOLD,
        criterios=criteria,
        hashes_entrada={},
    )
    inputs = [
        BASE / "etapa-1/mecanismos.csv",
        BASE / "etapa-1/fontes.csv",
        BASE / "etapa-2/mecanismos.csv",
        BASE / "etapa-3/mecanismos.csv",
        ROUND / "fila-documentos.sqlite",
        ROUND / "encerramento.json",
        ROOT / "var/validacao-final/regras-congeladas.csv",
    ]
    inputs.extend(ROOT / review['arquivo'] for review in audio_reviews if review.get('arquivo'))
    for path in inputs:
        with path.open("rb") as stream:
            summary["hashes_entrada"][str(path.relative_to(ROOT))] = hashlib.file_digest(
                stream, "sha256"
            ).hexdigest()
    reports.OUTPUT = OUTPUT
    reports.write_csv("mecanismos.csv", classifications)
    reports.write_csv("fontes.csv", source_rows)
    reports.write_csv("revisao-audio.csv", audio_reviews)
    gaps = [
        r for r in classifications if r["textos_confirmados"] and not r["endpoint_reutilizavel"]
    ]
    reports.write_csv("lacunas-proveniencia.csv", gaps)
    (OUTPUT / "resumo.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    overview = [dict(classificacao=LABELS[k], mecanismos=counts[k]) for k in LABELS]
    reports.workbook(
        {
            "Resumo": overview,
            "Fontes": source_rows,
            "Mecanismos": classifications,
            "Revisão áudio": audio_reviews,
            "Lacunas proveniência": gaps,
            "Critérios": [dict(criterio=s) for s in criteria],
        }
    )
    (OUTPUT / "consolidacao.xlsx").replace(OUTPUT / "classificacao-mecanismos.xlsx")
    lines = [
        "# Etapa 4 — classificação dos mecanismos",
        "",
        "| Uso proposto | Quantidade |",
        "|---|---:|",
        *[f"| {LABELS[k]} | {counts[k]} |" for k in LABELS],
        "",
        f"{summary['fontes_com_endpoint_frequente']} fontes com endpoint frequente e texto "
        "confirmado. Isso não garante cobertura integral ou atualização futura.",
        "",
        f"{summary['fontes_com_texto_mas_sem_endpoint_frequente']} fontes "
        "têm texto comprovado, "
        "mas ainda não têm caminho RSS/sitemap frequente individualmente comprovado. "
        "As lacunas estão explícitas, inclusive rótulos sintéticos como `feed:dominio`.",
        "",
        "## Critérios",
        "",
        *[f"- {s}" for s in criteria],
        "",
        "A planilha `classificacao-mecanismos.xlsx` reúne fontes, decisões por mecanismo, "
        "revisão de áudio e lacunas de proveniência. "
        "Nenhum novo processamento de rede foi iniciado.",
        "Não foi gerado nem aplicado um plano de coleta de produção nesta etapa.",
        "",
    ]
    (OUTPUT / "relatorio.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in ("criterios", "hashes_entrada")},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
