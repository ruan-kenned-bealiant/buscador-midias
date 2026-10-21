"""Etapa 3 offline: contribuição, sobreposição e trabalho observado por mecanismo."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import consolidate_round12 as reports
from buscador_midias.analysis.mechanism_metrics import exclusives, replay_coverage
from buscador_midias.domain.url import canonicalize_url, url_hash

ROOT = reports.ROOT
ROUND = reports.ROUND
FIRST = reports.OUTPUT
SECOND = ROOT / 'var/analise-rodada-12/etapa-2'
OUTPUT = ROOT / 'var/analise-rodada-12/etapa-3'
CRITERIA = [
    'Análise offline; nenhuma requisição, nova coleta, seleção ou exclusão de mecanismos.',
    'Candidatas datadas não são todas notícias úteis. Texto confirmado restringe-se à etapa 2.',
    'Deduplicação por URL canônica, não por texto ou equivalência editorial entre reportagens.',
    'Sobreposição/exclusividade é por fonte e entre raízes com evidência nesta fila, '
    'não entre todos os caminhos possíveis do site.',
    'Origens diretas são atribuições de primeira gravação no corpus. Não demonstram '
    'todos os links presentes nesses XMLs; não são usadas para afirmar exclusividade.',
    'Raízes e origens-filhas não são somadas como caminhos independentes na comparação.',
    'Tarefas por raiz medem trabalho de processamento. Documentos HTTP compartilhados '
    'são contados pelo endereço exato; o cache os reaproveita.',
    'Documentos distintos não equivalem a chamadas HTTP: faltam robots, redirects, '
    'retries e dados completos da primeira fase. Tempo por mecanismo não foi registrado.',
    'Custo ausente fica vazio, não zero. Nas 163 fontes anteriores à fila, a evidência '
    'individual pode estar incompleta.',
    'A simulação gulosa cobre somente o conjunto observado de candidatas por raiz. '
    'Não é ótima garantida, plano de produção ou previsão de redução de tempo.',
    'A simulação contabiliza documentos compartilhados entre raízes e ignora raízes '
    'sem ganho naquele retrato; não recomenda excluir RSS sem novidades na janela.',
    'NZ Herald permanece parcial. Resultados e custo de suas raízes são apenas os observados.',
    'Páginas com texto confirmado podem ser reconhecidas em qualquer raiz que contenha '
    'a mesma URL na mesma fonte, sem extrapolar para as candidatas não verificadas.',
]


def rows(path: Path) -> list[dict[str, str]]:
    """Lê um CSV produzido pelas etapas anteriores."""
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def mid(site: str, url: str) -> str:
    """Mantém a identidade da etapa 2."""
    return hashlib.sha256((site+'\n'+canonicalize_url(url)).encode()).hexdigest()


def main() -> None:
    """Reconciliada a coleta, mede conjuntos completos conhecidos e evidência textual amostral."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    state = reports.read_json(SECOND / 'execucao.json')
    assert state['status'] == 'completed' and state['paginas_concluidas'] == 16004
    source_first = {r['fonte']: r for r in rows(FIRST / 'fontes.csv')}
    first = {mid(r['fonte'], r['url']): r for r in rows(FIRST / 'mecanismos.csv')
             if r['tipo'] != 'bootstrap'}
    second = {mid(r['fonte'], r['mecanismo']): r for r in rows(SECOND / 'mecanismos.csv')}
    assert set(first) <= set(second)
    manifest = reports.read_json(ROUND / 'fila-manifesto.json')
    preserved = set(manifest['preserved_sources'])
    closure = reports.read_json(ROUND / 'encerramento.json')
    useful: dict[str, set[str]] = defaultdict(set)
    recent_useful: dict[str, set[str]] = defaultdict(set)
    results_digest = hashlib.sha256()
    for file in sorted((SECOND / 'resultados').glob('*.json')):
        payload = file.read_bytes()
        results_digest.update(file.name.encode())
        results_digest.update(hashlib.sha256(payload).digest())
        result = json.loads(payload)
        if result['texto_util_confirmado']:
            key = url_hash(result['url'])
            useful[result['fonte']].add(key)
            if result['publicacao_na_janela'] == 'sim':
                recent_useful[result['fonte']].add(key)

    candidates: dict[str, set[str]] = defaultdict(set)
    documents: dict[str, set[str]] = defaultdict(set)
    source_roots: dict[str, set[str]] = defaultdict(set)
    root_ids: dict[int, str] = {}
    bootstrap_done = 0
    queue = sqlite3.connect(f'file:{ROUND / "fila-documentos.sqlite"}?mode=ro', uri=True)
    queue.row_factory = sqlite3.Row
    for row in queue.execute('SELECT * FROM roots'):
        if row['kind'] == 'bootstrap':
            continue
        key = mid(row['site'], row['url'])
        root_ids[row['id']] = key
        source_roots[row['site']].add(key)
    for row in queue.execute("SELECT root_id,url FROM tasks WHERE state='done'"):
        task_key = root_ids.get(row['root_id'])
        if task_key is not None:
            documents[task_key].add(row['url'])
        else:
            bootstrap_done += 1
    for row in queue.execute('SELECT root_id,hash FROM evidence'):
        evidence_key = root_ids.get(row['root_id'])
        if evidence_key is not None:
            candidates[evidence_key].add(row['hash'])
    queue.close()

    # Primeira origem de gravação: apenas um limite inferior, nunca prova de exclusividade.
    direct: dict[str, set[str]] = defaultdict(set)
    with (ROUND / 'noticias-48h.csv').open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            direct[mid(row['site_key'], row['source_sitemap'])].add(url_hash(row['url']))

    exclusive: dict[str, set[str]] = {}
    shared: dict[str, dict[str, int]] = {}
    source_stats: dict[str, dict[str, Any]] = {}
    simulation: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []
    pair_seen: set[tuple[str, str]] = set()
    for site, keys in sorted(source_roots.items()):
        sets = {key: candidates[key] for key in keys}
        exclusive.update(exclusives(sets))
        inverted: dict[str, list[str]] = defaultdict(list)
        for key, values in sets.items():
            for value in values:
                inverted[value].append(key)
        for key in sorted(keys):
            counts: Counter[str] = Counter()
            for value in sets[key]:
                counts.update(other for other in inverted[value] if other != key)
            strongest = sorted(counts, key=lambda other: (-counts[other], other))[:5]
            shared[key] = {other: counts[other] for other in strongest}
            for other in strongest:
                pair = (min(key, other), max(key, other))
                if pair in pair_seen:
                    continue
                pair_seen.add(pair)
                intersection = counts[other]
                union = len(sets[key]) + len(sets[other]) - intersection
                pair_rows.append(dict(
                    fonte=site, mecanismo_a=second[key]['mecanismo'],
                    mecanismo_b=second[other]['mecanismo'], candidatas_em_comum=intersection,
                    candidatas_a=len(sets[key]), candidatas_b=len(sets[other]),
                    jaccard=round(intersection/union, 6),
                    nota='Entre os cinco maiores cruzamentos de pelo menos uma raiz',
                ))
        steps = replay_coverage(sets, documents)
        covered = set().union(*sets.values()) if sets else set()
        all_docs = set().union(*(documents[key] for key in keys)) if keys else set()
        processed = sum(int(first[key]['documentos_lidos']) for key in keys)
        assert processed == sum(len(documents[key]) for key in keys)
        assert not steps or steps[-1].covered_candidates == len(covered)
        replay_documents = steps[-1].read_documents if steps else 0
        assert replay_documents <= len(all_docs)
        for rank, step in enumerate(steps, 1):
            simulation.append(dict(
                fonte=site, ordem_retrospectiva=rank,
                mecanismo=second[step.mechanism]['mecanismo'],
                candidatas_acrescentadas=step.added_candidates,
                documentos_acrescentados=step.added_documents,
                candidatas_cobertas=step.covered_candidates,
                documentos_distintos_acumulados=step.read_documents,
                aviso='Simulação do retrato observado; '
                'não altera plano e não prevê cobertura futura',
            ))
        source_stats[site] = dict(
            raizes_comparadas=len(keys), raizes_com_candidatas=sum(bool(v) for v in sets.values()),
            candidatas_distintas_entre_raizes=len(covered),
            ocorrencias_de_candidatas_entre_raizes=sum(len(v) for v in sets.values()),
            textos_confirmados_entre_candidatas=len(covered & useful[site]),
            textos_confirmados_com_publicacao_na_janela=len(covered & recent_useful[site]),
            tarefas_xml_processadas=processed, documentos_xml_distintos=len(all_docs),
            repeticoes_de_processamento_mesmo_xml=processed-len(all_docs),
            raizes_simulacao=len(steps), documentos_simulacao=replay_documents,
        )

    mechanism_rows: list[dict[str, Any]] = []
    ordered = sorted(second.items(), key=lambda pair: (pair[1]['fonte'], pair[1]['mecanismo']))
    for key, row in ordered:
        site = row['fonte']
        cost = first.get(key)
        compared = key in source_roots.get(site, set())
        observed = candidates[key] if compared else direct[key]
        texts = observed & useful[site]
        unique = exclusive.get(key, set())
        tasks = int(cost['documentos_lidos']) if compared and cost else None
        best = next(iter(shared.get(key, {})), '')
        mechanism_rows.append(dict(
            fonte=site, mecanismo=row['mecanismo'], tipo=row['tipo'],
            categoria_etapa1=row['categoria_etapa1'], resultado_amostra=row['resultado_amostra'],
            conjunto_comparavel_por_raiz=compared,
            evidencia_conjunto='fila_por_raiz' if compared else
            'limite_inferior_primeira_origem' if observed else 'sem_conjunto_individual',
            candidatas_observadas=len(observed),
            exclusivas_entre_raizes=len(unique) if compared else None,
            compartilhadas_com_outra_raiz=len(observed)-len(unique) if compared else None,
            percentual_compartilhado=round(100*(len(observed)-len(unique))/len(observed), 2)
            if compared and observed else None,
            textos_confirmados_entre_candidatas=len(texts),
            textos_confirmados_exclusivos=len(unique & useful[site]) if compared else None,
            textos_confirmados_com_publicacao_na_janela=len(observed & recent_useful[site]),
            documentos_processados=tasks,
            candidatas_por_documento=round(len(observed)/tasks, 4) if tasks else None,
            documentos_por_candidata=round(tasks/len(observed), 4)
            if tasks and observed else None,
            documentos_pendentes=int(cost['documentos_pendentes']) if cost else None,
            falhas_documentos=int(cost['documentos_com_falha']) if compared and cost else None,
            maior_sobreposicao_com=second[best]['mecanismo'] if best else '',
            maior_sobreposicao_quantidade=shared[key][best] if best else 0,
            exemplo_textual=row['exemplo_textual'],
            alcance='parcial' if site in closure['incomplete_sources'] else
            'fontes anteriores à fila têm evidência incompleta' if site in preserved else
            'somente evidência registrada, sujeita a escopo, falhas e janela',
        ))

    source_rows: list[dict[str, Any]] = []
    for site, row in sorted(source_first.items()):
        stats = source_stats.get(site, {})
        source_rows.append(dict(
            fonte=site, nome=row['nome'], categoria_etapa1=row['categoria'],
            custo_individual_disponivel=site not in preserved,
            candidatas_exportadas_etapa1=int(row['candidatas_exportadas']),
            paginas_com_texto_confirmado_etapa2=len(useful[site]),
            **{field: stats.get(field) for field in [
                'raizes_comparadas', 'raizes_com_candidatas', 'candidatas_distintas_entre_raizes',
                'ocorrencias_de_candidatas_entre_raizes', 'textos_confirmados_entre_candidatas',
                'textos_confirmados_com_publicacao_na_janela', 'tarefas_xml_processadas',
                'documentos_xml_distintos', 'repeticoes_de_processamento_mesmo_xml',
                'raizes_simulacao', 'documentos_simulacao']},
            tarefas_incluindo_descoberta=int(row['documentos_lidos']) if site not in preserved
            else None,
            documentos_pendentes_preservados=int(row['documentos_pendentes']),
            limite='Parcial: NZ Herald' if site in closure['incomplete_sources'] else
            'Sem custo individual completo da fase anterior à fila' if site in preserved else
            'Comparação restrita às raízes observadas; não garante cobertura integral',
        ))

    total_tasks = sum(r['tarefas_xml_processadas'] for r in source_stats.values())
    assert total_tasks+bootstrap_done == closure['documents_completed']
    assert len(mechanism_rows) == len(second) == 40502
    assert len(source_rows) == 1294
    assert sum(len(v) for v in useful.values()) == 11782
    assert sum(len(v) for v in recent_useful.values()) <= 11782
    aggregate_fields = ['raizes_comparadas', 'raizes_com_candidatas',
                        'candidatas_distintas_entre_raizes',
                        'ocorrencias_de_candidatas_entre_raizes', 'tarefas_xml_processadas',
                        'documentos_xml_distintos', 'repeticoes_de_processamento_mesmo_xml',
                        'raizes_simulacao', 'documentos_simulacao']
    totals = {field: sum(r[field] for r in source_stats.values()) for field in aggregate_fields}
    compared_rows = [r for r in mechanism_rows if r['conjunto_comparavel_por_raiz']]
    summary = dict(
        rodada=12, etapa=3, gerado_em=datetime.now(UTC).isoformat(), fontes=1294,
        mecanismos_e_origens=len(mechanism_rows),
        mecanismos_sem_custo_individual=sum(r['documentos_processados'] is None
                                          for r in mechanism_rows),
        mecanismos_com_candidatas_sem_exclusivas_entre_raizes=sum(
            bool(r['candidatas_observadas']) and r['exclusivas_entre_raizes'] == 0
            for r in compared_rows),
        mecanismos_com_texto_confirmado_e_exclusivo_entre_raizes=sum(
            bool(r['textos_confirmados_exclusivos']) for r in compared_rows),
        textos_distintos_confirmados_etapa2=sum(len(v) for v in useful.values()),
        raizes_com_texto_em_alguma_candidata=sum(
            bool(r['textos_confirmados_entre_candidatas']) for r in compared_rows),
        resultados_etapa2_sha256_composto=results_digest.hexdigest(),
        total_tarefas_incluindo_descoberta=total_tasks+bootstrap_done,
        tarefas_bootstrap=bootstrap_done, totais_no_escopo_da_fila=totals,
        fontes_sem_custo_individual_completo=len(preserved),
        pares_exportados=len(pair_rows), criterios=CRITERIA,
        arquivos_entrada_sha256={},
    )
    inputs = [FIRST / 'fontes.csv', FIRST / 'mecanismos.csv', SECOND / 'mecanismos.csv',
              SECOND / 'resumo.json', SECOND / 'entradas.json', ROUND / 'noticias-48h.csv',
              ROUND / 'fila-documentos.sqlite', ROUND / 'encerramento.json',
              ROUND / 'fila-manifesto.json']
    for path in inputs:
        with path.open('rb') as stream:
            summary['arquivos_entrada_sha256'][str(path.relative_to(ROOT))] = hashlib.file_digest(
                stream, 'sha256').hexdigest()
    reports.OUTPUT = OUTPUT
    tables = [('fontes.csv', source_rows), ('mecanismos.csv', mechanism_rows),
              ('sobreposicoes.csv', pair_rows), ('simulacao-observada.csv', simulation)]
    for filename, data in tables:
        reports.write_csv(filename, data)
    (OUTPUT / 'resumo.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    overview = [dict(indicador=key, valor=value) for key, value in totals.items()]
    reports.workbook({'Resumo': overview, 'Fontes': source_rows, 'Mecanismos': mechanism_rows,
                      'Sobreposições': pair_rows, 'Simulação observada': simulation,
                      'Critérios': [dict(criterio=criterion) for criterion in CRITERIA]})
    (OUTPUT / 'consolidacao.xlsx').replace(OUTPUT / 'contribuicao-e-custo.xlsx')
    expensive = sorted(source_rows, key=lambda r: r['tarefas_incluindo_descoberta'] or 0,
                       reverse=True)[:10]
    report = [
        '# Etapa 3 — contribuição e custo observado', '',
        'Análise local da rodada 12. Nenhuma configuração de coleta foi alterada.', '',
        f"{totals['raizes_comparadas']:,} raízes comparadas, das quais "
        f"{totals['raizes_com_candidatas']:,} registraram candidatas na janela.", '',
        f"{totals['ocorrencias_de_candidatas_entre_raizes']:,} associações raiz/candidata "
        f"correspondem a {totals['candidatas_distintas_entre_raizes']:,} URLs distintas por fonte "
        'no escopo comparável. Não somar com as origens diretas. '
        'O conjunto difere do CSV de URLs da etapa 1 porque mantém associações por fonte '
        'e não contém toda a proveniência anterior à fila.', '',
        f"{totals['tarefas_xml_processadas']:,} tarefas de XML, mais {bootstrap_done:,} "
        f"tarefas de descoberta. São {totals['documentos_xml_distintos']:,} endereços de XML "
        'distintos por fonte; não representam contagem exata de chamadas HTTP.', '',
        '## Simulação retrospectiva', '',
        f"Uma ordenação gulosa cobre o mesmo conjunto de candidatas observado em "
        f"{totals['raizes_simulacao']:,} raízes e "
        f"{totals['documentos_simulacao']:,} documentos distintos por fonte. "
        'É uma análise do retrato já conhecido, não um plano selecionado ou promessa de tempo. '
        'Mecanismos sem resultados nessa janela podem ser importantes na próxima.', '',
        '## Fontes com mais trabalho registrado', '',
        '| Fonte | Tarefas, incluindo descoberta | Textos confirmados na etapa 2 |',
        '|---|---:|---:|',
        *[f"| {r['fonte']} | {r['tarefas_incluindo_descoberta']} | "
          f"{r['paginas_com_texto_confirmado_etapa2']} |" for r in expensive], '',
        '## Critérios e limites', '', *[f'- {criterion}' for criterion in CRITERIA], '',
        '## Arquivos', '',
        '- `contribuicao-e-custo.xlsx`: resumo, fontes, mecanismos, '
        'sobreposições, simulação e critérios.',
        '- `fontes.csv` e `mecanismos.csv`: métricas com custo ausente explicitamente vazio.',
        '- `sobreposicoes.csv`: cinco maiores cruzamentos observados por raiz, '
        'sem pares repetidos.',
        '- `simulacao-observada.csv`: ganho marginal e documentos compartilhados, por fonte.',
        '- `resumo.json`: totais reconciliados e hashes das entradas.', '',
    ]
    (OUTPUT / 'relatorio.md').write_text('\n'.join(report), encoding='utf-8')
    print(json.dumps({key: value for key, value in summary.items()
                      if key not in ('criterios', 'arquivos_entrada_sha256')},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
