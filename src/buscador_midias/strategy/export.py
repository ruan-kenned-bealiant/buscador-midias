"""Planilha de conferência das estratégias: uma linha por fonte, mais candidatos e seções.

A montagem das tabelas é pura (sem banco nem arquivo); gravar em ``.xlsx`` exige o extra ``xlsx``
(``openpyxl``) e em ``.csv`` não exige nada.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from buscador_midias.domain.site import Site
from buscador_midias.domain.strategy import CandidateReport, Measure, ProbeState, Role, Verdict
from buscador_midias.strategy.hints import SourceHints

SUMMARY_SHEET = "Estratégia"
CANDIDATES_SHEET = "Candidatos"
SECTIONS_SHEET = "Seções"
_WINDOW_DAYS = 2  # a janela de "recentes" são 48 h
_ROLE_NAMES = {Role.PRIMARY: "principal", Role.BACKUP: "reserva", Role.COMPLEMENT: "complemento"}
_ROLE_ORDER = {Role.PRIMARY: 0, Role.BACKUP: 1, Role.COMPLEMENT: 2}
_PERCENT_HEADERS = frozenset({"Cobertura do principal", "Cobertura do conjunto", "Cobertura"})
_PERCENT_HEADERS |= {"% do candidato"}
_WIDE_HEADERS = frozenset(
    {"Endereço principal", "Endereço da reserva", "Endereços dos complementos", "Endereço"}
)

SUMMARY_HEADERS = (
    "Fonte",
    "Chave",
    "Região",
    "País",
    "Tipo",
    "Situação",
    "Estratégia principal",
    "Endereço principal",
    "Recentes 48h (principal)",
    "Última matéria (principal)",
    "Seções (principal)",
    "Maior seção",
    "Seções vistas (todas as candidatas)",
    "Seções fora do conjunto",
    "Reserva",
    "Endereço da reserva",
    "Complementos",
    "Endereços dos complementos",
    "Cobertura do principal",
    "Cobertura do conjunto",
    "Matérias recentes (união 48h)",
    "Estimativa por dia",
    "Ref.: categoria",
    "Ref.: GDELT/dia",
    "Ref.: itens/dia no feed",
    "Motivo (sem estratégia)",
    "Sondada em",
    "Reavaliar a partir de",
    "Tentativas sem melhora",
)
CANDIDATE_HEADERS = (
    "Chave",
    "Fonte",
    "Papel",
    "Endereço",
    "Origem",
    "Veredito",
    "Tipo",
    "Itens",
    "Com data",
    "Recentes 48h",
    "Com título",
    "Seções",
    "Cobertura",
    "Nota",
    "Mais recente",
    "Requisições",
    "Detalhe",
    "Sondada em",
)
SECTION_HEADERS = (
    "Chave",
    "Fonte",
    "Papel",
    "Endereço",
    "Seção",
    "Matérias recentes",
    "% do candidato",
)
MAP_HEADERS = (
    "Chave",
    "Fonte",
    "Seção",
    "Matérias",
    "Coberta pelo conjunto",
    "Listada em",
    "Observação",
)
MAP_SHEET = "Mapa de seções"
_FLAT_MIN_SECTIONS = 10
_FLAT_MAX_RATIO = 0.5
_FLAT_NOTE = (
    "O primeiro trecho do caminho muda a cada matéria (URL plana): não são seções, "
    "então não dá para mapear o site por elas"
)


@dataclass(frozen=True, slots=True)
class Table:
    """Uma aba: cabeçalho e linhas."""

    headers: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True, slots=True)
class StrategyTables:
    """As abas da planilha."""

    summary: Table
    candidates: Table
    sections: Table
    section_map: Table


def build_tables(
    sites: Sequence[Site],
    candidates: Sequence[tuple[str, CandidateReport, datetime]],
    states: Mapping[str, ProbeState],
    hints: SourceHints,
) -> StrategyTables:
    """Monta as abas a partir do que está gravado.

    Args:
        sites: As fontes, na ordem em que aparecem na planilha.
        candidates: Todos os candidatos gravados (fonte, relatório e quando foi sondado).
        states: O estado de reavaliação de cada fonte já sondada.
        hints: O levantamento, só para as colunas de referência.

    Returns:
        A aba de resumo (uma linha por fonte, mesmo as não sondadas), a de candidatos e a de seções
        dos candidatos escolhidos.
    """
    by_site: dict[str, list[tuple[CandidateReport, datetime]]] = {}
    for key, report, probed_at in candidates:
        by_site.setdefault(key, []).append((report, probed_at))
    ordered = {
        site.key: sorted(
            by_site.get(site.key, []),
            key=lambda pair: (
                _ROLE_ORDER.get(pair[0].role, 3) if pair[0].role else 3,
                -pair[0].score,
                pair[0].url,
            ),
        )
        for site in sites
    }
    maps = {s.key: _section_map(s, [r for r, _ in ordered[s.key]]) for s in sites}
    summary = [
        _summary_row(s, ordered[s.key], states.get(s.key), hints, maps[s.key]) for s in sites
    ]
    cand_rows = [
        _candidate_row(s, report, probed_at) for s in sites for report, probed_at in ordered[s.key]
    ]
    section_rows = [
        row
        for s in sites
        for report, _ in ordered[s.key]
        if report.role
        for row in _sections(s, report)
    ]
    return StrategyTables(
        Table(SUMMARY_HEADERS, tuple(summary)),
        Table(CANDIDATE_HEADERS, tuple(cand_rows)),
        Table(SECTION_HEADERS, tuple(section_rows)),
        Table(MAP_HEADERS, tuple(row for s in sites for row in maps[s.key].rows)),
    )


def write_xlsx(tables: StrategyTables, path: Path) -> None:
    """Grava a planilha ``.xlsx`` (abas com cabeçalho em negrito, filtro e primeiras colunas fixas).

    Raises:
        RuntimeError: Se o extra ``xlsx`` (``openpyxl``) não está instalado.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as error:  # pragma: no cover - depende do ambiente
        raise RuntimeError("para gerar .xlsx instale o extra: pip install '.[xlsx]'") from error

    book = Workbook()
    book.remove(book.active)
    sheets = (
        (SUMMARY_SHEET, tables.summary, "C2"),
        (CANDIDATES_SHEET, tables.candidates, "C2"),
        (SECTIONS_SHEET, tables.sections, "A2"),
        (MAP_SHEET, tables.section_map, "C2"),
    )
    for title, table, freeze in sheets:
        sheet = book.create_sheet(title)
        sheet.append(list(table.headers))
        for row in table.rows:
            sheet.append([_cell(value) for value in row])
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="305496")
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for index, header in enumerate(table.headers, start=1):
            letter = get_column_letter(index)
            sheet.column_dimensions[letter].width = _width(header, table, index - 1)
            for cell in sheet[letter][1:]:
                if header in _PERCENT_HEADERS:
                    cell.number_format = "0%"
                elif isinstance(cell.value, datetime):
                    cell.number_format = "yyyy-mm-dd hh:mm"
                if header in _WIDE_HEADERS:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.freeze_panes = freeze
        sheet.auto_filter.ref = sheet.dimensions
    book.save(path)


def write_csv(tables: StrategyTables, path: Path) -> None:
    """Grava o resumo em ``path`` e as outras abas ao lado (``-candidatos`` e ``-secoes``)."""
    targets = (
        (path, tables.summary),
        (path.with_name(f"{path.stem}-candidatos{path.suffix}"), tables.candidates),
        (path.with_name(f"{path.stem}-secoes{path.suffix}"), tables.sections),
        (path.with_name(f"{path.stem}-mapa-secoes{path.suffix}"), tables.section_map),
    )
    for target, table in targets:
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(table.headers)
            writer.writerows([[_cell(value) for value in row] for row in table.rows])


def _summary_row(
    site: Site,
    rows: Sequence[tuple[CandidateReport, datetime]],
    state: ProbeState | None,
    hints: SourceHints,
    section_map: _SectionMap,
) -> tuple[Any, ...]:
    reports = [report for report, _ in rows]
    primary = next((r for r in reports if r.role is Role.PRIMARY), None)
    backup = next((r for r in reports if r.role is Role.BACKUP), None)
    complements = [r for r in reports if r.role is Role.COMPLEMENT]
    reference = hints.reference(site)
    if primary is not None:
        situation = "lenta" if primary.verdict is Verdict.SLOW else "ok"
    elif state is not None or rows:
        situation = "sem estratégia"
    else:
        situation = "não sondada"
    measure = primary.measure if primary else None
    top = measure.top_sections[0] if measure and measure.top_sections else None
    base = (
        max(measure.recent, 1) if measure and measure.recent else (measure.items if measure else 1)
    )
    probed_at = state.probed_at if state else (max(t for _, t in rows) if rows else None)
    return (
        site.name,
        site.key,
        site.region,
        site.country,
        site.source_type,
        situation,
        primary.kind.value if primary and primary.kind else "",
        primary.url if primary else "",
        measure.recent if measure else "",
        measure.newest if measure else "",
        measure.sections if measure else "",
        f"{top[0]} ({top[1] / base:.0%})" if top else "",
        section_map.seen if rows else "",
        section_map.outside if rows else "",
        backup.kind.value if backup and backup.kind else "",
        backup.url if backup else "",
        len(complements) if primary else "",
        "\n".join(c.url for c in complements),
        primary.coverage if primary else "",
        primary.cover_share if primary else "",
        primary.union_recent if primary else "",
        primary.union_recent // _WINDOW_DAYS if primary else "",
        reference.get("categoria", ""),
        reference.get("gdelt_24h", ""),
        reference.get("itens_24h_feed", ""),
        state.detail if state and primary is None else "",
        probed_at,
        state.refresh_at if state else "",
        state.streak if state else "",
    )


def _candidate_row(site: Site, report: CandidateReport, probed_at: datetime) -> tuple[Any, ...]:
    m = report.measure
    return (
        site.key,
        site.name,
        _ROLE_NAMES.get(report.role, "") if report.role else "",
        report.url,
        report.origin.value,
        report.verdict.value,
        report.kind.value if report.kind else "",
        m.items if m else "",
        m.dated if m else "",
        m.recent if m else "",
        m.titled if m else "",
        m.sections if m else "",
        report.coverage if report.coverage is not None else "",
        round(report.score, 1),
        m.newest if m else "",
        report.requests,
        report.detail,
        probed_at,
    )


def _sections(site: Site, report: CandidateReport) -> list[tuple[Any, ...]]:
    m = report.measure
    if m is None:
        return []
    base = m.recent or m.items or 1
    role = _ROLE_NAMES.get(report.role, "") if report.role else ""
    return [
        (site.key, site.name, role, report.url, name, count, count / base)
        for name, count in m.top_sections
    ]


@dataclass(frozen=True, slots=True)
class _SectionMap:
    """O mapa de seções de uma fonte: as linhas da aba e os números do resumo."""

    rows: tuple[tuple[Any, ...], ...]
    seen: int | str
    outside: int | str


def _section_map(site: Site, reports: Sequence[CandidateReport]) -> _SectionMap:
    """Une as seções de todas as candidatas e marca as que o conjunto escolhido lista.

    A seção é o primeiro trecho do caminho: em sites de URL plana (``/titulo-da-materia``) ele
    muda a cada matéria e o mapa não vale, o que se avisa em vez de listar milhares de "seções".
    Só as maiores seções de cada candidata ficam gravadas (``MAX_TOP_SECTIONS``), e o aviso diz
    quando há mais.
    """
    measured = [r for r in reports if r.measure is not None and r.measure.top_sections]
    if not measured:
        return _SectionMap((), 0, 0)
    primary = next((r for r in measured if r.role is Role.PRIMARY), measured[0])
    assert primary.measure is not None
    if _is_flat(primary.measure):
        row = (site.key, site.name, "(URL plana)", "", "", "", _FLAT_NOTE)
        return _SectionMap((row,), "URL plana", "URL plana")
    found: dict[str, tuple[int, set[str], bool]] = {}
    for report in measured:
        assert report.measure is not None
        label = _ROLE_NAMES[report.role] if report.role else ""
        for name, count in report.measure.top_sections:
            best, labels, covered = found.get(name, (0, set(), False))
            labels.add(label)
            found[name] = (max(best, count), labels, covered or report.role is not None)
    ordered = sorted(found.items(), key=lambda item: (-item[1][0], item[0]))
    rows: list[tuple[Any, ...]] = []
    total = primary.measure.sections
    if total > len(primary.measure.top_sections):
        note = (
            f"{total} seções no total; só as {len(primary.measure.top_sections)} maiores aparecem"
        )
        rows.append((site.key, site.name, "", "", "", "", note))
    for name, (count, labels, covered) in ordered:
        named = sorted(label for label in labels if label)
        where = ", ".join(named) if named else "só em candidata não escolhida"
        rows.append((site.key, site.name, name, count, "sim" if covered else "não", where, ""))
    outside = sum(1 for _, (_, _, covered) in ordered if not covered)
    return _SectionMap(tuple(rows), len(ordered), outside)


def _is_flat(measure: Measure) -> bool:
    """Se há mais "seções" que a metade das matérias: o caminho não traz seção."""
    base = measure.recent or measure.items or 1
    return measure.sections >= _FLAT_MIN_SECTIONS and measure.sections > base * _FLAT_MAX_RATIO


def _cell(value: Any) -> Any:
    """Valor de célula: datas viram horário local sem fuso (a planilha não guarda fuso)."""
    if isinstance(value, datetime):
        return value.astimezone().replace(tzinfo=None)
    return value


def _width(header: str, table: Table, index: int) -> float:
    """Largura da coluna: a do maior conteúdo, limitada."""
    if header in _WIDE_HEADERS:
        return 60
    longest = max((len(str(row[index])) for row in table.rows), default=0)
    return float(min(max(len(header), longest) + 2, 40))
