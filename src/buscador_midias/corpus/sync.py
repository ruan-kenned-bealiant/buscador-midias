"""Sincronização dos achados com o corpus: grava o que é novo e acompanha a evidência.

A cada execução:

1. Confere se o que já foi gravado ainda está no corpus (um teste ou uma limpeza do KyX pode apagar
   linhas) e refaz o que sumiu.
2. Grava os achados ainda não gravados ou cuja evidência mudou desde a última gravação (por
   exemplo, ``pending`` virou ``captured``).
3. Retém, por regra, o que não deve entrar no corpus: achados só pela manchete e achados abaixo da
   confiança mínima. O que ficou retido é contado, para a regra ser visível.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from buscador_midias.corpus.article import CorpusArticle, build_corpus_article
from buscador_midias.corpus.writer import CorpusWriter
from buscador_midias.domain.site import Site
from buscador_midias.state.store import CorpusTotals, StateStore

DEFAULT_BATCH_SIZE = 50
_VERIFY_CHUNK = 500


@dataclass(frozen=True, slots=True)
class CorpusSummary:
    """Resultado de uma execução da gravação no corpus.

    Attributes:
        run_id: Identificador da execução no estado (0 no ``dry_run``, que não grava nada).
        totals: Totais.
    """

    run_id: int
    totals: CorpusTotals


def sync_corpus(
    store: StateStore,
    writer: CorpusWriter,
    *,
    sites: Mapping[str, Site],
    now: datetime,
    include_headline_only: bool = False,
    min_confidence: float = 0.0,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    dry_run: bool = False,
) -> CorpusSummary:
    """Grava no corpus os achados pendentes e registra o que foi feito.

    Args:
        store: Estado persistente.
        writer: Porta de gravação no corpus.
        sites: Fontes do catálogo por ``Site.key`` (dão o país do artigo).
        now: Instante gravado nos registros.
        include_headline_only: Grava também achados baseados só na manchete.
        min_confidence: Confiança mínima da análise para gravar.
        batch_size: Artigos por chamada ao gravador.
        limit: Máximo de achados nesta execução (``None``: todos).
        dry_run: Só calcula o que seria gravado; não grava no corpus nem no estado.

    Returns:
        O resumo da execução.
    """
    written = store.corpus_written_hashes()
    present = _present(writer, written)
    missing = [url_hash for url_hash in written if url_hash not in present]
    if missing and not dry_run:
        store.forget_corpus_writes(missing)

    candidates = store.pending_corpus_sync(
        include_headline_only=include_headline_only, min_confidence=min_confidence, limit=limit
    )
    withheld = store.count_corpus_withheld(
        include_headline_only=include_headline_only, min_confidence=min_confidence
    )
    if dry_run:
        return CorpusSummary(
            0, CorpusTotals(candidates=len(candidates), withheld=withheld, rewritten=len(missing))
        )

    run_id = store.start_corpus_run(now)
    names = writer.source_names()
    created = existing = updated = entities = keywords = failed = 0
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        articles = [build_corpus_article(c, sites.get(c.site_key), names) for c in batch]
        for article, outcome in zip(articles, writer.write(articles), strict=True):
            if outcome.error is not None:
                failed += 1
                continue
            _record(store, article, outcome.article_id, outcome.created, run_id, now)
            created += outcome.created
            existing += not outcome.created
            updated += outcome.evidence_updated
            entities += outcome.entities
            keywords += outcome.keywords
    totals = CorpusTotals(
        candidates=len(candidates),
        created=created,
        existing=existing,
        evidence_updated=updated,
        entities=entities,
        keywords=keywords,
        failed=failed,
        withheld=withheld,
        rewritten=len(missing),
    )
    store.finish_corpus_run(run_id, now, totals)
    return CorpusSummary(run_id, totals)


def _present(writer: CorpusWriter, hashes: list[str]) -> set[str]:
    """Quais dos hashes ainda existem no corpus (consulta em lotes)."""
    found: set[str] = set()
    for start in range(0, len(hashes), _VERIFY_CHUNK):
        found |= writer.existing(hashes[start : start + _VERIFY_CHUNK])
    return found


def _record(
    store: StateStore,
    article: CorpusArticle,
    article_id: int | None,
    created: bool,
    run_id: int,
    now: datetime,
) -> None:
    """Registra a gravação, com o estado da evidência que foi para o corpus."""
    store.record_corpus_write(
        article.url_hash,
        article_id=article_id,
        created=created,
        evidence_state=article.evidence.status,
        corpus_run_id=run_id,
        now=now,
    )
