"""Consolida a auditoria finita e confere as restaurações no PostgreSQL, sem nova busca."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime

from psycopg.rows import dict_row

from buscador_midias.catalog.postgres import connect_postgres
from map_editorial_priority import credentials
from recover_all_transports import OUT, read_gz, stamp


def failure(row):
    """Separa transporte, formato e comprovação editorial; não transforma 200 em sucesso."""
    if row["confirmed"]:
        return "noticia_textual_confirmada"
    result = row.get("result", {})
    attempts = row.get("attempts", [])
    if (
        result.get("retry_after_seconds")
        or any(
            "cooldown" in str(a.get("error", "")).lower()
            or any(o.get("retry_after_seconds") for o in a.get("observations", []))
            for a in attempts
        )
        or "cooldown" in str(row.get("confirmation", {}).get("error", "")).lower()
    ):
        return "adiado_por_pausa"
    if result.get("usable_for_news"):
        return "confirmacao_independente_falhou"
    if result.get("xml_valid"):
        if result.get("news_urls", 0) == 0:
            return "xml_sem_noticias_na_amostra"
        if any(s.get("accessed") for s in result.get("samples", [])):
            return "artigos_sem_texto_confirmado"
        return "xml_valido_artigos_inacessiveis"
    attempts = row.get("attempts", [])
    if any(a.get("format_error") for a in attempts):
        return "formato_incompativel"
    return "falha_de_acesso_ou_orcamento"


def main():
    """Produz CSVs e resumo somente quando todos os alvos têm resultado persistido."""
    manifest = read_gz(OUT / "manifest.json.gz")
    progress = json.loads((OUT / "progress.json").read_text())
    if not progress.get("completed"):
        raise SystemExit("A rodada ainda está em andamento.")
    actions, failures, profiles, protocols, statuses, purposes = (Counter() for _ in range(6))
    sources = defaultdict(Counter)
    names = {}
    confirmed = []
    rows = []
    for item in manifest["links"]:
        row = read_gz(OUT / "results" / f"{item['id']}.json.gz")
        result = row.get("result", {})
        decision_path = OUT / "applied" / f"{item['id']}.json.gz"
        decision = read_gz(decision_path) if decision_path.exists() else {}
        category = failure(row)
        failures[category] += 1
        if decision:
            actions[decision["action"]] += 1
            if decision.get("purpose"):
                purposes[decision["purpose"]] += 1
        names[item["source_id"]] = item["source_name"]
        counts = sources[item["source_id"]]
        counts["testados"] += 1
        if row["confirmed"]:
            confirmed.append(row)
            profiles[row["profile"]] += 1
            counts["confirmados"] += 1
        action = decision.get("action", "sem_alteracao")
        counts[action] += 1
        counts["site_reativado"] += int(decision.get("source_activated", False))
        for attempt in row["attempts"]:
            for event in attempt.get("observations", []):
                statuses[str(event.get("status"))] += 1
        events = result.get("transport_events", [])
        for event in events:
            protocols[event.get("protocol", "desconhecido")] += 1
        rows.append(
            dict(
                link_id=item["id"],
                source_id=item["source_id"],
                fonte=item["source_name"],
                url=item["url"],
                confirmado=row["confirmed"],
                transporte=row.get("profile", ""),
                categoria=category,
                resultado=result.get("outcome", ""),
                noticias_textuais_amostra=result.get("text_samples", 0),
                acao=action,
                cadastro_canonico_id=decision.get("duplicate_link_id", ""),
                finalidade=decision.get("purpose", ""),
                noticia_exemplo=row.get("confirmation", {}).get("url", ""),
                observado_em=row["finished_at"],
            )
        )
    with (OUT / "links.csv").open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    source_rows = [dict(source_id=i, fonte=names[i], **c) for i, c in sorted(sources.items())]
    fields = sorted({k for r in source_rows for k in r})
    with (OUT / "fontes.csv").open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, restval=0)
        writer.writeheader()
        writer.writerows(source_rows)
    with connect_postgres(credentials()) as db:
        db.row_factory = dict_row
        db.execute("SET TRANSACTION READ ONLY")
        totals = db.execute("""SELECT count(*) links_ativos,
            count(distinct l.source_id) fontes_ativas
            FROM midias_manager.links l JOIN midias_manager.sites s ON s.id=l.source_id
            WHERE l.active=1 AND l.deleted_at IS NULL AND s.active=1 AND s.deleted_at IS NULL
            """).fetchone()
        settings = db.execute(
            "SELECT config FROM midias_manager.ingest_settings WHERE id=1"
        ).fetchone()
        last_run = db.execute(
            "SELECT id,state,created_at FROM midias_manager.ingest_runs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        applied_rows = [read_gz(p) for p in (OUT / "applied").glob("*.json.gz")]
        canonical_rows = [
            read_gz(p) for p in (OUT / "canonical-recheck" / "applied").glob("*.json.gz")
        ]
        canonical_actions = Counter(r["action"] for r in canonical_rows)
        ids = [
            r["link_id"]
            for r in applied_rows
            if r["action"] in ("restored", "created", "transport_configured")
        ]
        ids += [r["link_id"] for r in canonical_rows if r["action"] == "transport_configured"]
        verified = db.execute(
            """SELECT l.id,l.active,l.deleted_at,l.access_transport,l.last_test,
            s.active source_active,s.deleted_at source_deleted,
            EXISTS(SELECT 1 FROM midias_manager.link_test_results t WHERE t.link_id=l.id
                AND t.test_id=l.last_test->>'test_id') history_saved
            FROM midias_manager.links l JOIN midias_manager.sites s ON s.id=l.source_id
            WHERE l.id=ANY(%s)""",
            (ids,),
        ).fetchall()
    violations = []
    for row in verified:
        test = row["last_test"]
        audit = test.get("independent_confirmation", {}).get("audit", {})
        if (
            not row["active"]
            or row["deleted_at"]
            or not row["source_active"]
            or row["source_deleted"]
            or not row["history_saved"]
            or not test.get("usable_for_news")
            or test.get("access_transport") != row["access_transport"]
            or not audit.get("texto_util_confirmado")
        ):
            violations.append(row["id"])
    summary = dict(
        run_id=manifest["id"],
        created_at=manifest["created_at"],
        finished_at=progress["at"],
        report_at=stamp(),
        total=len(rows),
        confirmed=len(confirmed),
        confirmed_sources=len({r["link"]["source_id"] for r in confirmed}),
        actions=dict(actions),
        canonical_supplement=dict(canonical_actions),
        failure_groups=dict(failures),
        profiles=dict(profiles),
        purposes=dict(purposes),
        observed_protocols=dict(protocols),
        sample_statuses=dict(statuses),
        source_reactivations=sum(c["site_reativado"] for c in sources.values()),
        catalogue=totals,
        database_verified=len(verified),
        violations=violations,
        automatic_ingestion_enabled=(settings or {}).get("config", {}).get("enabled"),
        last_ingestion_run=last_run,
        note=(
            "Confirmação amostral sem filtro de data. Não mede cobertura completa "
            "nem garante atualizações diárias. Protocolos são observações, não atribuição causal."
        ),
    )
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if violations or len(verified) != len(ids):
        raise SystemExit("A auditoria encontrou divergências de aplicação.")
    duration = datetime.fromisoformat(progress["at"]) - datetime.fromisoformat(
        manifest["created_at"]
    )
    action_labels = {
        "restored": "Restaurados e ativados",
        "created": "Novos links cadastrados",
        "transport_configured": "Links ativos com acesso configurado",
        "duplicate_preserved": "Duplicatas preservadas sem nova ativação",
    }
    table = "\n".join(
        f"| {action_labels.get(k, k)} | {v:,} |".replace(",", ".") for k, v in actions.items()
    )
    doc = f"""# Recuperação de links por transporte HTTP — 26/10/2026

Rodada `{manifest["id"]}` concluída em {str(duration).split(".")[0]}.

Foram examinados **{len(rows):,} links** de uma lista congelada: excluídos automaticamente,
links ativos com falhas anteriores e os candidatos XML oficiais do piloto.
Houve confirmação textual independente em **{len(confirmed):,} links**,
pertencentes a **{summary["confirmed_sources"]} fontes**.
Itens ainda adiados por pausa: **{failures["adiado_por_pausa"]}**. Permanecem
sem conclusão; o total examinado não significa que todos chegaram à rede.

Um link comprovado pode duplicar outro já cadastrado; por isso confirmação e restauração
têm números diferentes.

| Decisão aplicada | Links |
| --- | ---: |
{table}

O complemento canônico configurou mais **{canonical_actions["transport_configured"]} links**
ativos que estavam no transporte padrão, sem restaurar duplicatas. Seus checkpoints
estão em `canonical-recheck/`.

Foram reativadas **{summary["source_reactivations"]} fontes** automaticamente inativadas.
O catálogo ficou com **{totals["links_ativos"]:,} links ativos**
em **{totals["fontes_ativas"]} fontes ativas**.
Esses totais incluem os links existentes antes desta rodada, que não foram todos revalidados aqui.

## O que foi testado

Até três perfis: curl com Chrome HTTP/2, Chrome HTTP/1.1 e Firefox HTTP/2.
Cada perfil tem até 25 segundos, 16 requisições, quatro documentos XML,
profundidade máxima dez, resposta de até 8 MiB e três artigos distribuídos na amostra.
404/410, Retry-After, RSS vazio e falhas puramente editoriais encerram tentativas desnecessárias.
Há até 32 grupos de hosts em paralelo. Nos grupos grandes, até dois links ficam
em trânsito; Prensa Libre e Information permitem até quatro após centenas de
aprovações sem pausas. A cauda da OCCRP permitiu até três leituras em trânsito.
Os grupos pequenos são sequenciais.
O espaçamento mínimo por host é de 300 ms.

A aprovação exige XML compatível, artigo acessível e texto útil. Uma nova sessão, sem
cache da primeira leitura, confirma um artigo. Essa prova independente pode ser
reutilizada por até cinco minutos entre feeds da mesma fonte/perfil/artigo, preservando
o horário original e registrando `reused_at`. Datas antigas são aceitas para comprovar
funcionamento. Isso não comprova que o feed atualiza diariamente nem que todas as suas
notícias estejam acessíveis. Áudio dedicado, CAPTCHA e páginas sem texto não são aprovados.
Pausas curtas podem ser aguardadas antes do próximo link, com teto acumulado de
180 segundos por grupo de host. Os itens somente adiados tiveram uma retomada limitada;
os registros anteriores estão em `before-cooldown-retry/`. Itens ainda adiados
continuam sem conclusão e não são classificados como definitivamente inválidos.
A OCCRP recebeu uma correção específica de reconhecimento do corpo/data editorial;
os falsos negativos anteriores estão preservados em `before-occrp-template-fix/`.

## Aplicação e uso

A coluna `links.access_transport` conserva o perfil comprovado. Teste do gerenciador,
Saúde das fontes e futuras tarefas de descoberta usam esse mesmo valor. Ele pode ser
editado em **Forma de acesso**. O mecanismo continua responsável por interpretar XML e
selecionar notícias; o perfil configura a conexão HTTP.

Exclusões manuais e alterações concorrentes são preservadas. URLs equivalentes não são
restauradas em duplicidade. Nos links aplicados, testes anteriores e novos permanecem
em `link_test_results`,
com eventos de auditoria e confirmação independente. Os demais resultados desta
rodada ficam nos checkpoints comprimidos e nos CSVs. A auditoria SQL conferiu
{len(verified):,} links alterados; divergências: {len(violations)}.

Arquivos históricos e índices extensos ficam identificados como `archive`/`review`,
se não havia classificação anterior, para evitar busca histórica na rotina diária.
Finalidades dos links aplicados: `{dict(purposes)}`.

Não houve Bright Data, OpenRouter, análise de contexto ou nova rodada de ingestão.
A agenda automática continua no estado verificado em `summary.json`.
HTTP/2 não resolveu todos os sites: persistem bloqueios, endereços inválidos, feeds vazios,
falhas de rede e páginas sem texto. Ganhos também incluem revalidação atual e correção de
extração; não são atribuídos exclusivamente ao protocolo HTTP/2.

## Artefatos e reprodução

`var/gerenciador/recuperacao-transportes-20261026/` contém manifesto, resultados
comprimidos por link, decisões aplicadas, `links.csv`, `fontes.csv` e `summary.json`.
Na rodada principal, os corpos positivos são preservados comprimidos até 160 MiB; os
resultados estruturados não são cortados por esse limite. Há checkpoints para retomada.

```bash
PYTHONPATH=src .venv/bin/python scripts/recover_all_transports.py
PYTHONPATH=src .venv/bin/python scripts/apply_transport_recovery.py --apply
PYTHONPATH=src .venv/bin/python scripts/recheck_canonical_transports.py
PYTHONPATH=src .venv/bin/python scripts/report_transport_recovery.py
```

Executar novamente reutiliza a lista e os checkpoints desta rodada. Não amplia
automaticamente o universo de links nem abre novas rodadas de descoberta.
"""
    for value in (len(rows), len(confirmed), len(verified), totals["links_ativos"]):
        doc = doc.replace(f"{value:,}", f"{value:,}".replace(",", "."))
    (OUT.parents[2] / "docs/RECUPERACAO_TRANSPORTES.md").write_text(doc)


if __name__ == "__main__":
    main()
