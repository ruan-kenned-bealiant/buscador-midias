"""Etapa 5: planos reutilizáveis e pacote local do painel; não executa buscas."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import consolidate_round12 as reports
from buscador_midias.domain.url import canonicalize_url
from buscador_midias.sites.loader import load_sites
from buscador_midias.strategy.discovery_plan import load_discovery_plan
from buscador_midias.strategy.manual import load_manual_rules

ROOT = reports.ROOT
ROUND = reports.ROUND
INPUT = ROOT / "var/analise-rodada-12/etapa-4"
OUTPUT = ROOT / "var/analise-rodada-12/etapa-5"


def save(name: str, data: Any) -> None:
    """Escreve um artefato da entrega, sem tocar nas entradas."""
    (OUTPUT / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def rows(path: Path) -> list[dict[str, str]]:
    """Lê um CSV de classificação."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def main() -> None:
    """Exporta e valida planos, catálogo completo e protocolo comparativo ainda não executado."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    mechanisms = rows(INPUT / "mecanismos.csv")
    sources = rows(INPUT / "fontes.csv")
    catalog = load_sites(ROOT / "var/validacao-final/catalogo-congelado.csv")
    sites = {s.key: s for s in catalog.sites}
    rules = load_manual_rules(ROOT / "var/validacao-final/regras-congeladas.csv")
    plans: dict[str, list[dict[str, Any]]] = {}
    for category in ("frequente", "periodico"):
        groups: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in mechanisms:
            if row["classificacao"] == category:
                assert row["endpoint_reutilizavel"] == "True"
                groups[row["fonte"]].append(row)
        plan = []
        for site, members in sorted(groups.items()):
            forced = rules.for_site(sites[site]).force
            ordered = sorted(
                members,
                key=lambda r: (
                    r["mecanismo"] not in forced if forced else False,
                    {"feed": 0, "news_sitemap": 1, "sitemap": 2}[r["tipo_reutilizavel"]],
                    not bool(r["documentos_processados"]),
                    int(r["documentos_processados"] or 0),
                    r["mecanismo"],
                ),
            )
            if forced:
                assert ordered[0]["mecanismo"] in forced
            plan.append(
                dict(
                    chave=site,
                    mecanismos=[
                        dict(url=r["mecanismo"], kind=r["tipo_reutilizavel"]) for r in ordered
                    ],
                )
            )
        save(f"plano-{category}.json", plan)
        loaded = load_discovery_plan(OUTPUT / f"plano-{category}.json", sites)
        assert sum(len(items) for items in loaded.values()) == sum(len(g) for g in groups.values())
        plans[category] = plan

    # Controle comparativo: mesmas fontes do frequente e união das entradas já conhecidas,
    # para não premiar o plano novo por conter uma origem que o controle não possua.
    comparison_sources = {row["chave"] for row in plans["frequente"]}
    reference: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for plan in [reports.read_json(ROUND / "plano-todos.json"), *plans.values()]:
        for row in plan:
            if row["chave"] in comparison_sources:
                for mechanism in row["mecanismos"]:
                    reference[row["chave"]][canonicalize_url(mechanism["url"])] = mechanism
    control = [
        dict(chave=site, mecanismos=list(values.values()))
        for site, values in sorted(reference.items())
    ]
    save("plano-referencia-comparacao.json", control)
    load_discovery_plan(OUTPUT / "plano-referencia-comparacao.json", sites)
    counts = Counter(row["classificacao"] for row in mechanisms)
    assert sum(len(row["mecanismos"]) for row in plans["frequente"]) == counts["frequente"]
    assert sum(len(row["mecanismos"]) for row in plans["periodico"]) == counts["periodico"]
    assert len(sources) == 1294

    for row in sources:
        row["pais"] = sites[row["fonte"]].country
        row["url"] = sites[row["fonte"]].url
    save("fontes.json", sources)
    for name in ("mecanismos.csv", "fontes.csv", "lacunas-proveniencia.csv"):
        shutil.copyfile(INPUT / name, OUTPUT / name)
    shutil.copyfile(
        INPUT / "classificacao-mecanismos.xlsx", OUTPUT / "classificacao-mecanismos.xlsx"
    )
    comparison: dict[str, Any] = dict(
        status="preparada_nao_executada",
        executar_automaticamente=False,
        plano_controle="plano-referencia-comparacao.json",
        plano_selecionado="plano-frequente.json",
        fontes_comuns=len(comparison_sources),
        janela_horas=48,
        instante_referencia=None,
        profundidade_maxima=10,
        bright_data=False,
        openrouter=False,
        requisitos=[
            "Definir um único instante de referência e a mesma janela de 48h para os dois braços.",
            "Usar somente fontes comuns e endpoints explícitos; "
            "não adicionar estratégias ou fallback.",
            "Processar todos os complementos do plano; "
            "não usar o padrão de três complementos da CLI.",
            "Mesmos workers, intervalo por host, robots, timeout de conexão e regras de escopo.",
            "Estados e checkpoints isolados, sem alterar resultados históricos da rodada 12.",
            "Registrar por endpoint e fonte: início/fim, "
            "chamadas reais incluindo robots/redirects, "
            "cache, documentos, bytes, falhas, candidatas e texto confirmado.",
            "Separar primeiro acesso de cache quente e usar as mesmas respostas para replay; "
            "não comparar execuções com cache desigual como ganho de rede.",
            "Comparar conjuntos de URLs canônicas e publicação confirmada, não apenas contagens.",
            "Notícias exclusivas do controle exigem verificar conteúdo e ajustar a seleção.",
            "A CLI discover atual pode acrescentar backups e cascata; não usá-la como executor "
            "estrito sem adaptação e validação. Estes planos já passam no leitor de planos.",
        ],
    )
    save("comparacao.json", comparison)
    (OUTPUT / "comparacao.md").write_text(
        "# Busca comparativa preparada\n\nNão executada. Nenhum agendamento criado.\n\n"
        f"Comparar o plano frequente com o controle nas mesmas {len(comparison_sources)} fontes. "
        "As demais fontes continuam no catálogo e nos planos periódicos/inconclusivos; "
        "não atribuir a redução de fontes a ganho de desempenho.\n\n"
        + "\n".join(f"{i}. {text}" for i, text in enumerate(comparison["requisitos"], 1))
        + "\n\nO plano periódico é um complemento separado, sem frequência em horas definida. "
        "A comparação futura medirá se há perda de notícias úteis "
        "e o custo real dos dois braços.\n",
        encoding="utf-8",
    )
    manifest: dict[str, Any] = dict(
        rodada=12,
        etapa=5,
        gerado_em=datetime.now(UTC).isoformat(),
        estado="entrega_estatica_nao_aplicada",
        fontes_catalogo=1294,
        fontes_frequentes=len(plans["frequente"]),
        fontes_periodicas=len(plans["periodico"]),
        mecanismos_frequentes=counts["frequente"],
        mecanismos_periodicos=counts["periodico"],
        inconclusivos=counts["inconclusivo"],
        exclusoes_textuais=counts["excluir"],
        lacunas_proveniencia=sum(row["lacuna_proveniencia"] == "True" for row in sources),
        origem_classificacao=str(INPUT.relative_to(ROOT)),
        comparacao_executada=False,
        agendamento_criado=False,
        arquivos_sha256={},
    )
    for file in sorted(OUTPUT.iterdir()):
        if file.is_file() and file.name != "manifesto.json":
            with file.open("rb") as stream:
                manifest["arquivos_sha256"][file.name] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    save("manifesto.json", manifest)
    print(
        json.dumps(
            {k: v for k, v in manifest.items() if k != "arquivos_sha256"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
