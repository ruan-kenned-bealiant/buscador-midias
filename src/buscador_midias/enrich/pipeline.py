"""Enriquecimento das URLs novas: texto, análise de contexto, critério e evidência.

Para cada URL nova, em blocos:

1. **Texto** pelo download direto (HTTP educado, sem Bright Data). Site que bloqueia, matéria que
   sumiu ou página inutilizável não impedem a análise: ela segue só pela manchete, com a base
   marcada como ``headline`` (confiança menor). Erro de rede é tentado de novo nas rodadas
   seguintes, até esgotar as tentativas.
2. **Análise de contexto** pelo LLM (entidades, temas, fato adverso). Resposta ausente ou inválida
   deixa a URL pendente; nunca vira "sem achado".
3. **Critério** (``criteria.meets_criteria``): só a matéria lida com entidade identificável segue.
4. **Evidência** das que atendem ao critério (adversas ou não): o HTML do download direto é
   oferecido ao capturador, que o reaproveita se for confiável. O Bright Data (pago) só entra
   para o achado adverso, quando o HTML direto não serve (ou sempre, no modo ``brightdata``).

A evidência é capturada **bloco a bloco**, logo depois da análise do bloco, e o HTML é solto em
seguida: com milhares de matérias por dia, acumular as páginas até o fim estouraria a memória.
Mesma ordem do KyX: primeiro o contexto, e a captura só depois de haver entidade.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from buscador_midias.analysis.context import (
    DEFAULT_BATCH_SIZE,
    AnalysisItem,
    ContextAnalyzer,
    analyze_all,
)
from buscador_midias.analysis.headline import headline_text
from buscador_midias.analysis.openrouter import Usage
from buscador_midias.criteria import meets_criteria
from buscador_midias.extraction.fetch import PageFetch, PageStatus
from buscador_midias.extraction.html import extract_article
from buscador_midias.extraction.reliability import looks_like_challenge_page, looks_like_error_page
from buscador_midias.state.store import (
    AnalysisRecord,
    ContentRecord,
    EnrichPending,
    EnrichTotals,
    EvidenceRecord,
    FindingUrl,
    StateStore,
)

DEFAULT_CHUNK_SIZE = 60
DEFAULT_MAX_FETCH_ATTEMPTS = 3
MIN_ANALYSIS_TEXT_CHARS = 200
_NO_TEXT_STATUSES = frozenset({"blocked", "not_found", "robots_disallowed"})


class ArticleSource(Protocol):
    """Porta: baixa um artigo."""

    def fetch(self, url: str) -> PageFetch:
        """Baixa a URL."""
        ...


class EvidenceSink(Protocol):
    """Porta: captura a evidência de um achado."""

    def capture(
        self, finding: FindingUrl, direct: PageFetch | None, *, expected_headline: str
    ) -> EvidenceRecord:
        """Captura e devolve o estado da evidência."""
        ...


@dataclass(frozen=True, slots=True)
class EnrichSummary:
    """Resultado de uma execução do enriquecimento.

    Attributes:
        run_id: Identificador da execução no estado.
        totals: Totais e consumo.
    """

    run_id: int
    totals: EnrichTotals


@dataclass(slots=True)
class _Counters:
    """Contadores mutáveis de uma execução (só a thread principal os altera)."""

    fetched_ok: int = 0
    fetched_blocked: int = 0
    fetched_other: int = 0
    analyzed: int = 0
    findings: int = 0
    evidence_reused: int = 0
    evidence_brightdata: int = 0
    evidence_failed: int = 0


def enrich_pending(
    store: StateStore,
    *,
    articles: ArticleSource,
    analyzer: ContextAnalyzer,
    capturer: EvidenceSink,
    now: datetime,
    model: str,
    prompt_version: str,
    evidence_mode: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    workers: int = 4,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    limit: int | None = None,
    evidence_limit: int | None = None,
    max_fetch_attempts: int = DEFAULT_MAX_FETCH_ATTEMPTS,
    analyzer_usage: Callable[[], Usage] | None = None,
    bright_data_requests: Callable[[], int] | None = None,
) -> EnrichSummary:
    """Extrai, analisa e captura a evidência das URLs novas que ainda não foram analisadas.

    Args:
        store: Estado persistente.
        articles: Porta de download direto.
        analyzer: Porta de análise de contexto (thread-safe).
        capturer: Porta de captura de evidência.
        now: Instante gravado nos registros.
        model: Modelo da análise (auditoria).
        prompt_version: Versão do prompt (auditoria).
        evidence_mode: ``auto`` (reaproveita o HTML se confiável) ou ``brightdata``.
        batch_size: Matérias por chamada ao LLM.
        workers: Downloads, chamadas ao LLM e capturas simultâneos.
        chunk_size: URLs por bloco (limita a memória com HTML e dá progresso incremental).
        limit: Máximo de URLs pendentes nesta execução (``None``: todas).
        evidence_limit: Máximo de evidências capturadas nesta execução (``None``: todas).
        max_fetch_attempts: Após tantos erros de rede, a análise segue só pela manchete.
        analyzer_usage: Consumo acumulado do analisador; a execução registra a diferença.
        bright_data_requests: Total de chamadas pagas ao Bright Data; idem.

    Returns:
        O resumo da execução.
    """
    run_id = store.start_enrich_run(
        now, model=model, prompt_version=prompt_version, evidence_mode=evidence_mode
    )
    usage_before = analyzer_usage() if analyzer_usage else Usage()
    paid_before = bright_data_requests() if bright_data_requests else 0
    pending = store.pending_enrichment(limit)
    counters = _Counters()
    attempted: set[str] = set()  # evidências já tentadas nesta execução: não repetir em outro bloco

    for start in range(0, len(pending), chunk_size):
        pages: dict[str, PageFetch] = {}  # HTML só do bloco; some quando o bloco termina
        _process_chunk(
            store,
            pending[start : start + chunk_size],
            articles=articles,
            analyzer=analyzer,
            run_id=run_id,
            model=model,
            prompt_version=prompt_version,
            now=now,
            batch_size=batch_size,
            workers=workers,
            max_fetch_attempts=max_fetch_attempts,
            counters=counters,
            pages=pages,
        )
        _capture_evidence(
            store,
            articles=articles,
            capturer=capturer,
            now=now,
            evidence_mode=evidence_mode,
            workers=workers,
            limit=evidence_limit,
            counters=counters,
            pages=pages,
            attempted=attempted,
        )

    # Pendências de execuções anteriores (e o caso de não haver nenhuma URL nova a analisar).
    _capture_evidence(
        store,
        articles=articles,
        capturer=capturer,
        now=now,
        evidence_mode=evidence_mode,
        workers=workers,
        limit=evidence_limit,
        counters=counters,
        pages={},
        attempted=attempted,
    )

    usage = (analyzer_usage() if analyzer_usage else Usage()).minus(usage_before)
    totals = EnrichTotals(
        urls=len(pending),
        fetched_ok=counters.fetched_ok,
        fetched_blocked=counters.fetched_blocked,
        fetched_other=counters.fetched_other,
        analyzed=counters.analyzed,
        findings=counters.findings,
        evidence_reused=counters.evidence_reused,
        evidence_brightdata=counters.evidence_brightdata,
        evidence_failed=counters.evidence_failed,
        bright_data_requests=(bright_data_requests() if bright_data_requests else 0) - paid_before,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cost_usd=usage.cost_usd,
        retries=usage.retries,
        errors=usage.errors,
    )
    store.finish_enrich_run(run_id, now, totals)
    return EnrichSummary(run_id=run_id, totals=totals)


def _process_chunk(
    store: StateStore,
    chunk: Sequence[EnrichPending],
    *,
    articles: ArticleSource,
    analyzer: ContextAnalyzer,
    run_id: int,
    model: str,
    prompt_version: str,
    now: datetime,
    batch_size: int,
    workers: int,
    max_fetch_attempts: int,
    counters: _Counters,
    pages: dict[str, PageFetch],
) -> None:
    """Baixa, extrai e analisa um bloco de URLs; guarda o HTML das que precisarão de evidência."""
    to_fetch = [item for item in chunk if item.content_status in (None, "error")]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fetched = list(pool.map(lambda item: articles.fetch(item.url), to_fetch))
    fetched_by_hash: dict[str, PageFetch] = {}
    for item, page in zip(to_fetch, fetched, strict=True):
        fetched_by_hash[item.url_hash] = page
        _count_fetch(counters, page)
        store.record_content(_content_record(item, page, now), now=now)

    items: list[AnalysisItem] = []
    owners: dict[int, EnrichPending] = {}
    for pending in chunk:
        content = store.get_content(pending.url_hash)
        if (
            content is not None
            and content.status == "error"
            and store.fetch_attempts(pending.url_hash) < max_fetch_attempts
        ):
            continue  # erro de rede: tenta de novo na próxima execução
        candidate = _analysis_item(len(items) + 1, pending, content)
        if not candidate.headline and not candidate.text:
            _record_no_finding(store, pending.url_hash, run_id=run_id, now=now, counters=counters)
            continue
        items.append(candidate)
        owners[candidate.id] = pending

    for verdict in analyze_all(analyzer, items, batch_size=batch_size, workers=workers):
        if not verdict.decided:
            continue  # sem resposta aproveitável: segue pendente
        pending = owners[verdict.id]
        store.record_analysis(
            AnalysisRecord(
                url_hash=pending.url_hash,
                finding=verdict.finding,
                themes=verdict.themes,
                entities=verdict.entities,
                adverse_fact=verdict.adverse_fact,
                summary=verdict.summary,
                confidence=verdict.confidence,
                basis=verdict.basis,
            ),
            enrich_run_id=run_id,
            model=model,
            prompt_version=prompt_version,
            now=now,
        )
        counters.analyzed += 1
        if verdict.finding:
            counters.findings += 1
        if (
            meets_criteria(basis=verdict.basis, entities=verdict.entities)
            and pending.url_hash in fetched_by_hash
        ):
            pages[pending.url] = fetched_by_hash[pending.url_hash]


def _count_fetch(counters: _Counters, page: PageFetch) -> None:
    """Contabiliza o desfecho de um download."""
    if page.status is PageStatus.OK:
        counters.fetched_ok += 1
    elif page.status in (PageStatus.BLOCKED, PageStatus.ROBOTS_DISALLOWED):
        counters.fetched_blocked += 1
    else:
        counters.fetched_other += 1


def _content_record(item: EnrichPending, page: PageFetch, now: datetime) -> ContentRecord:
    """Resultado do download e da extração, pronto para gravar."""
    if page.status is not PageStatus.OK:
        return ContentRecord(
            url_hash=item.url_hash,
            status=page.status.value,
            http_status=page.http_status,
            final_url=page.final_url,
            title=None,
            description=None,
            lede=None,
            text_chars=0,
            published_at=None,
            language=None,
            error=page.error,
        )
    content = extract_article(page.content, now=now, content_type=page.content_type)
    expected = headline_text(item.url, item.title)
    problem = _unusable_reason(
        page.content, content.text_chars, expected, content.title_candidates, content.is_article
    )
    if problem is not None:
        return ContentRecord(
            url_hash=item.url_hash,
            status="unusable",
            http_status=page.http_status,
            final_url=page.final_url,
            title=None,
            description=None,
            lede=None,
            text_chars=content.text_chars,
            published_at=None,
            language=None,
            error=problem,
        )
    return ContentRecord(
        url_hash=item.url_hash,
        status="ok",
        http_status=page.http_status,
        final_url=page.final_url,
        title=content.title,
        description=content.description,
        lede=content.text,
        text_chars=content.text_chars,
        published_at=content.published_at,
        language=content.language,
        error=None,
    )


def _unusable_reason(
    html: bytes,
    text_chars: int,
    expected_headline: str,
    titles: Sequence[str],
    is_article: bool,
) -> str | None:
    """Motivo de a página não servir de texto para a análise (ou ``None`` se serve)."""
    if looks_like_error_page(html, expected_title=expected_headline):
        return "página de erro"
    if looks_like_challenge_page(" ".join(titles), text_chars=text_chars):
        return "desafio de bot"
    if not is_article:
        return "não é uma matéria (sem sinais de artigo)"
    if text_chars < MIN_ANALYSIS_TEXT_CHARS:
        return "texto insuficiente"
    return None


def _analysis_item(
    item_id: int, pending: EnrichPending, content: ContentRecord | None
) -> AnalysisItem:
    """Item para o analisador: texto da página quando houver; senão só a manchete."""
    if content is not None and content.status == "ok" and content.lede:
        return AnalysisItem(
            id=item_id,
            domain=pending.site_key,
            headline=content.title or headline_text(pending.url, pending.title),
            description=content.description or "",
            text=content.lede,
            basis="text",
        )
    return AnalysisItem(
        id=item_id,
        domain=pending.site_key,
        headline=headline_text(pending.url, pending.title),
        description="",
        text="",
        basis="headline",
    )


def _record_no_finding(
    store: StateStore, url_hash: str, *, run_id: int, now: datetime, counters: _Counters
) -> None:
    """Sem manchete nem texto não há o que analisar: grava "sem achado" sem chamar o modelo."""
    store.record_analysis(
        AnalysisRecord(url_hash, False, (), (), "", "", 0.0, "headline"),
        enrich_run_id=run_id,
        model="none",
        prompt_version="none",
        now=now,
    )
    counters.analyzed += 1


def _capture_evidence(
    store: StateStore,
    *,
    articles: ArticleSource,
    capturer: EvidenceSink,
    now: datetime,
    evidence_mode: str,
    workers: int,
    limit: int | None,
    counters: _Counters,
    pages: dict[str, PageFetch],
    attempted: set[str],
) -> None:
    """Captura a evidência das matérias pendentes que ainda não foram tentadas nesta execução.

    ``limit`` vale para a execução toda: ``attempted`` guarda o que já foi tentado (e conta no
    limite), o que também impede que uma falha do bloco 1 seja retentada nos blocos seguintes.
    """
    remaining = None if limit is None else limit - len(attempted)
    if remaining is not None and remaining <= 0:
        return
    findings = [f for f in store.pending_evidence() if f.url_hash not in attempted]
    if remaining is not None:
        findings = findings[:remaining]
    attempted.update(f.url_hash for f in findings)

    def direct_page(finding: FindingUrl) -> PageFetch | None:
        if evidence_mode != "auto":
            return None
        if finding.url in pages:
            return pages[finding.url]
        content = store.get_content(finding.url_hash)
        if content is not None and content.status in _NO_TEXT_STATUSES:
            return PageFetch(finding.url, PageStatus(content.status), content.http_status)
        return articles.fetch(finding.url)

    def capture(finding: FindingUrl) -> EvidenceRecord:
        return capturer.capture(
            finding,
            direct_page(finding),
            expected_headline=headline_text(finding.url, finding.title),
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(capture, findings))
    for record in records:
        store.record_evidence(record, now=now)
        if record.status != "captured":
            counters.evidence_failed += 1
        elif record.provider == "direct":
            counters.evidence_reused += 1
        else:
            counters.evidence_brightdata += 1
