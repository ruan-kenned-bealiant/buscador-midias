"""Passo da triagem por LLM: dá veredito às URLs novas ainda pendentes e grava no estado."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from buscador_midias.state.store import PendingUrl, StateStore, TriageRecord, TriageRunTotals
from buscador_midias.triage.headline import headline_text
from buscador_midias.triage.openrouter import Usage
from buscador_midias.triage.relevance import (
    DEFAULT_BATCH_SIZE,
    Decision,
    HeadlineClassifier,
    HeadlineItem,
    classify_all,
)


@dataclass(frozen=True, slots=True)
class TriageSummary:
    """Resultado de uma execução da triagem.

    Attributes:
        run_id: Identificador da execução no estado.
        totals: Totais e consumo.
    """

    run_id: int
    totals: TriageRunTotals

    @property
    def headlines(self) -> int:
        """Manchetes avaliadas."""
        return self.totals.headlines

    @property
    def kept(self) -> int:
        """Manchetes mantidas."""
        return self.totals.kept

    @property
    def discarded(self) -> int:
        """Manchetes descartadas."""
        return self.totals.discarded

    @property
    def pending(self) -> int:
        """Manchetes que seguem sem veredito."""
        return self.totals.pending


def triage_pending(
    store: StateStore,
    classifier: HeadlineClassifier,
    *,
    now: datetime,
    model: str,
    prompt_version: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    workers: int = 4,
    limit: int | None = None,
    usage_source: Callable[[], Usage] | None = None,
) -> TriageSummary:
    """Dá veredito às URLs ``new`` sem veredito e registra a execução (inclusive o custo).

    Só vereditos definitivos (``keep``/``discard``) são gravados: o que falhar no provedor continua
    pendente e é retomado na próxima execução, e nunca é descartado por isso. URLs sem manchete
    aproveitável (ex.: ``/videos/2255890``) são mantidas sem chamar o modelo.

    Args:
        store: Estado persistente.
        classifier: Porta de classificação (thread-safe).
        now: Instante gravado nos vereditos.
        model: Nome do modelo (auditoria).
        prompt_version: Versão do prompt (auditoria).
        batch_size: Manchetes por chamada.
        workers: Chamadas simultâneas.
        limit: Máximo de pendentes a avaliar nesta execução (``None``: todas).
        usage_source: Devolve o consumo acumulado do classificador; a execução registra a
            diferença entre antes e depois.

    Returns:
        O resumo da execução.
    """
    run_id = store.start_triage_run(now, model=model, prompt_version=prompt_version)
    before = usage_source() if usage_source else Usage()
    pending = store.pending_triage(limit)

    records: list[TriageRecord] = []
    items: list[HeadlineItem] = []
    by_id: dict[int, PendingUrl] = {}
    for url in pending:
        text = headline_text(url.url, url.title)
        if not text:
            records.append(TriageRecord(url.url_hash, Decision.KEEP.value, (), "sem manchete"))
            continue
        item = HeadlineItem(id=len(items) + 1, domain=url.site_key, text=text)
        items.append(item)
        by_id[item.id] = url

    unresolved = 0
    for verdict in classify_all(classifier, items, batch_size=batch_size, workers=workers):
        if verdict.decision is Decision.UNKNOWN:
            unresolved += 1
            continue
        records.append(
            TriageRecord(
                by_id[verdict.id].url_hash, verdict.decision.value, verdict.themes, verdict.reason
            )
        )

    store.record_triage(records, triage_run_id=run_id, now=now)
    after = usage_source() if usage_source else Usage()
    totals = _totals(records, unresolved, after.minus(before))
    store.finish_triage_run(run_id, now, totals)
    return TriageSummary(run_id=run_id, totals=totals)


def _totals(records: list[TriageRecord], unresolved: int, usage: Usage) -> TriageRunTotals:
    """Totais da execução a partir dos vereditos gravados e do consumo."""
    kept = sum(1 for r in records if r.decision == Decision.KEEP.value)
    return TriageRunTotals(
        headlines=len(records) + unresolved,
        kept=kept,
        discarded=len(records) - kept,
        pending=unresolved,
        calls=usage.calls,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cost_usd=usage.cost_usd,
        retries=usage.retries,
        errors=usage.errors,
    )
