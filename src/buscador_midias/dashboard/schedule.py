"""Agenda das execuções, para o painel mostrar o que já rodou e o que vem a seguir.

O sistema não tem agendador próprio (as rodadas são manuais ou disparadas por um cron/script
externo). Quem agenda pode registrar as execuções previstas em um JSON; sem ele, o painel estima a
próxima rodada pela cadência observada.

Formato do arquivo::

    {"jobs": [{"name": "Rodada 2/5", "kind": "discovery", "at": "2026-10-15T17:22:00-03:00",
               "note": "aprox."}]}

``kind`` é ``discovery``, ``triage``, ``enrich`` ou ``corpus``; ``at`` é ISO 8601 com fuso.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from buscador_midias.state.store import CorpusRun, EnrichRun, RunRecord, TriageRun

GRACE = timedelta(minutes=10)
DONE_WINDOW = timedelta(minutes=30)
MIN_RUNS_TO_ESTIMATE = 3
MIN_CADENCE = timedelta(minutes=30)
DONE_SHOWN = 3
_KINDS = frozenset({"discovery", "triage", "enrich", "corpus"})


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """Uma execução prevista.

    Attributes:
        name: Nome mostrado no painel.
        kind: ``discovery``, ``triage``, ``enrich`` ou ``corpus``.
        at: Horário previsto (com fuso).
        note: Observação livre (ex.: "aprox.").
    """

    name: str
    kind: str
    at: datetime
    note: str = ""


def load_schedule(path: Path | None) -> list[ScheduledJob]:
    """Lê as execuções previstas; arquivo ausente ou inválido vira lista vazia.

    Entradas malformadas são ignoradas uma a uma, para um erro de digitação não esconder o resto.

    Args:
        path: Caminho do JSON, ou ``None`` para não usar agenda.

    Returns:
        As execuções válidas, na ordem do arquivo.
    """
    if path is None or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = payload.get("jobs") if isinstance(payload, dict) else None
    jobs = [_job(entry) for entry in entries] if isinstance(entries, list) else []
    return [job for job in jobs if job is not None]


def build_schedule(
    *,
    path: Path | None,
    discovery_runs: list[RunRecord],
    triage_runs: list[TriageRun],
    now: datetime,
    enrich_runs: list[EnrichRun] | None = None,
    corpus_runs: list[CorpusRun] | None = None,
) -> dict[str, Any]:
    """Monta a agenda do painel: cada tarefa com o estado e a contagem regressiva.

    Estados: ``done`` (houve execução do tipo na janela do horário), ``upcoming`` (ainda não
    chegou a hora), ``due`` (a hora chegou há menos de 10 min), ``overdue`` (passou da tolerância
    sem execução). Só as últimas tarefas concluídas aparecem.

    Args:
        path: JSON de agenda (opcional).
        discovery_runs: Rodadas de descoberta já registradas.
        triage_runs: Execuções da triagem já registradas.
        now: Instante de referência.
        enrich_runs: Execuções do enriquecimento já registradas.
        corpus_runs: Execuções da gravação no corpus já registradas.

    Returns:
        ``{"source": "arquivo" | "estimado" | None, "jobs": [...]}``.
    """
    jobs = load_schedule(path)
    source: str | None = "arquivo" if jobs else None
    if not jobs:
        estimate = _estimate_next(discovery_runs)
        jobs = [estimate] if estimate else []
        source = "estimado" if estimate else None

    started = {
        "discovery": [run.started_at for run in discovery_runs],
        "triage": [run.started_at for run in triage_runs],
        "enrich": [run.started_at for run in enrich_runs or []],
        "corpus": [run.started_at for run in corpus_runs or []],
    }
    rows = [_row(job, started.get(job.kind, []), now) for job in sorted(jobs, key=lambda j: j.at)]
    done = [row for row in rows if row["state"] == "done"]
    hidden = {id(row) for row in done[:-DONE_SHOWN]} if len(done) > DONE_SHOWN else set()
    return {"source": source, "jobs": [row for row in rows if id(row) not in hidden]}


def _job(entry: Any) -> ScheduledJob | None:
    """Uma tarefa do arquivo, ou ``None`` se estiver malformada."""
    if not isinstance(entry, dict):
        return None
    try:
        at = datetime.fromisoformat(str(entry["at"]))
        name, kind = str(entry["name"]), str(entry["kind"])
    except (KeyError, ValueError):
        return None
    if kind not in _KINDS or at.tzinfo is None:
        return None
    return ScheduledJob(name=name, kind=kind, at=at, note=str(entry.get("note", "")))


def _estimate_next(runs: list[RunRecord]) -> ScheduledJob | None:
    """Próxima rodada estimada pela mediana dos intervalos, se a cadência parece agendada."""
    if len(runs) < MIN_RUNS_TO_ESTIMATE:
        return None
    starts = [run.started_at for run in runs]
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
    median = timedelta(seconds=statistics.median(gap.total_seconds() for gap in gaps))
    if median < MIN_CADENCE:
        return None
    return ScheduledJob(
        name="Próxima rodada (estimada)",
        kind="discovery",
        at=starts[-1] + median,
        note="estimada pela cadência observada",
    )


def _row(job: ScheduledJob, started: list[datetime], now: datetime) -> dict[str, Any]:
    """A tarefa com o estado em relação ao horário atual e às execuções já feitas."""
    ran = any(job.at - GRACE <= start <= job.at + DONE_WINDOW for start in started)
    if ran:
        state = "done"
    elif job.at > now:
        state = "upcoming"
    elif now - job.at <= GRACE:
        state = "due"
    else:
        state = "overdue"
    return {
        "name": job.name,
        "kind": job.kind,
        "at": job.at.isoformat(),
        "note": job.note,
        "state": state,
        "seconds_until": int((job.at - now).total_seconds()),
    }
