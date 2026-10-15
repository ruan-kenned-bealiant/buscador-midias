"""Pontuação dos rótulos humanos: precisão do corpus e recall estimado da triagem e da análise.

Cada rótulo responde "esta notícia entra no corpus?" (nomeia pessoa ou organização identificável).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from buscador_midias.labeling.csvio import read_rows
from buscador_midias.labeling.export import KEY_FILE
from buscador_midias.labeling.verdicts import NO, UNCERTAIN, UNLABELED, YES, parse_verdict
from buscador_midias.state.store import LABEL_GROUPS, StateStore

_Z = 1.96  # 95%


def wilson_interval(successes: int, total: int) -> tuple[float, float]:
    """Intervalo de confiança de 95% de Wilson para uma proporção (``(0, 1)`` sem amostra)."""
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    denominator = 1 + _Z**2 / total
    center = (p + _Z**2 / (2 * total)) / denominator
    half = _Z * math.sqrt(p * (1 - p) / total + _Z**2 / (4 * total**2)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


@dataclass(frozen=True, slots=True)
class GroupStats:
    """Rótulos de um grupo.

    Attributes:
        group: Nome do grupo.
        population: Quantas URLs o grupo tem no estado.
        yes: Rotuladas como "entra no corpus".
        no: Rotuladas como "não entra".
        uncertain: Rotuladas como incertas (ficam fora das taxas).
        unlabeled: Exportadas e ainda sem rótulo.
    """

    group: str
    population: int
    yes: int
    no: int
    uncertain: int
    unlabeled: int

    @property
    def labeled(self) -> int:
        """Rótulos que entram nas taxas (sim + não)."""
        return self.yes + self.no

    @property
    def yes_rate(self) -> float | None:
        """Fração que entra no corpus entre as rotuladas (``None`` sem rótulos)."""
        return self.yes / self.labeled if self.labeled else None

    @property
    def interval(self) -> tuple[float, float]:
        """Intervalo de 95% da taxa."""
        return wilson_interval(self.yes, self.labeled)


@dataclass(frozen=True, slots=True)
class LabelReport:
    """Medidas obtidas dos rótulos humanos.

    Attributes:
        groups: Estatísticas por grupo.
        corpus_precision: Do que o modelo mandou para o corpus, quanto entra de fato.
        triage_recall: Do que deveria entrar (estimado), quanto a triagem manteve.
        entity_recall: Do que deveria entrar entre as mantidas, quanto a análise achou com entidade.
        pipeline_recall: Do que deveria entrar (estimado), quanto chegou ao corpus (triagem e
            análise juntas).
        unknown_ids: Linhas com id que não consta na chave (ignoradas).
    """

    groups: dict[str, GroupStats]
    corpus_precision: float | None
    triage_recall: float | None
    entity_recall: float | None
    pipeline_recall: float | None
    unknown_ids: int


def score_labels(store: StateStore, folder: Path) -> LabelReport:
    """Lê os lotes preenchidos e calcula precisão e recall estimados.

    O recall estimado multiplica a taxa de "entra" de cada grupo pelo tamanho do grupo no estado:
    os grupos são amostras estratificadas, não a população toda. Os números valem tanto quanto o
    tamanho das amostras (veja ``GroupStats.interval``).

    Args:
        store: Estado (para o tamanho de cada grupo).
        folder: Pasta com ``chave.csv`` e os ``rotulagem-*.csv`` preenchidos.

    Returns:
        O relatório.
    """
    groups_by_id = {row["id"]: row["grupo"] for row in read_rows(folder / KEY_FILE)}
    verdicts: dict[str, str] = {}
    unknown = 0
    for path in sorted(folder.glob("rotulagem-*.csv")):
        for row in read_rows(path):
            label_id = row.get("id", "")
            if label_id not in groups_by_id:
                unknown += 1
                continue
            verdicts[label_id] = parse_verdict(row.get("veredito", ""))

    population = store.label_population()
    stats: dict[str, GroupStats] = {}
    for group in LABEL_GROUPS:
        values = [verdicts[i] for i, g in groups_by_id.items() if g == group and i in verdicts]
        stats[group] = GroupStats(
            group=group,
            population=population.get(group, 0),
            yes=values.count(YES),
            no=values.count(NO),
            uncertain=values.count(UNCERTAIN),
            unlabeled=values.count(UNLABELED),
        )

    rates = {g: s.yes_rate for g, s in stats.items()}
    precision = rates["in_corpus"]
    if any(rate is None for rate in rates.values()):
        return LabelReport(stats, precision, None, None, None, unknown)
    should_enter = {g: (rates[g] or 0.0) * stats[g].population for g in LABEL_GROUPS}
    total = sum(should_enter.values())
    kept = should_enter["in_corpus"] + should_enter["kept_no_entity"]
    return LabelReport(
        groups=stats,
        corpus_precision=precision,
        triage_recall=kept / total if total else None,
        entity_recall=should_enter["in_corpus"] / kept if kept else None,
        pipeline_recall=should_enter["in_corpus"] / total if total else None,
        unknown_ids=unknown,
    )


def format_label_report(report: LabelReport) -> str:
    """Texto legível do relatório de rotulagem."""
    names = {
        "in_corpus": "mandadas ao corpus pelo modelo",
        "kept_no_entity": "mantidas sem entidade",
        "triage_discarded": "descartadas na triagem",
    }
    lines = []
    for group, stats in report.groups.items():
        rate = "sem rótulos"
        if stats.yes_rate is not None:
            low, high = stats.interval
            rate = f"{stats.yes_rate:.0%} entram no corpus (IC95% {low:.0%}-{high:.0%})"
        lines.append(
            f"{names[group]}: {stats.labeled} rotuladas de {stats.population} no estado | {rate} | "
            f"incertas {stats.uncertain}, em branco {stats.unlabeled}"
        )

    def pct(value: float | None) -> str:
        return f"{value:.0%}" if value is not None else "n/d (faltam rótulos em algum grupo)"

    lines += [
        f"precisão do corpus: {pct(report.corpus_precision)}",
        f"recall estimado da triagem: {pct(report.triage_recall)}",
        f"recall estimado da análise de entidades (entre as mantidas): {pct(report.entity_recall)}",
        f"recall estimado do fluxo todo (triagem + análise): {pct(report.pipeline_recall)}",
    ]
    if report.unknown_ids:
        lines.append(f"aviso: {report.unknown_ids} linha(s) com id fora da chave foram ignoradas")
    return "\n".join(lines)
