"""Descoberta incremental: a cada rodada, só o que ainda não vimos e que de fato é notícia nova."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from buscador_midias.corpus.index import CorpusIndex, SynchronizedCorpusIndex, find_known_urls
from buscador_midias.discovery.explore import (
    DEFAULT_MAX_SITEMAP_FETCHES,
    Exploration,
    ProbeStatus,
    explore_site,
)
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.domain.site import Site
from buscador_midias.domain.sitemap import SitemapRef
from buscador_midias.net.fetcher import Fetcher
from buscador_midias.state.store import SiteRunRecord, StateStore, UrlKind
from buscador_midias.triage.l0 import l0_filter
from buscador_midias.triage.l1 import L1Filter

DEFAULT_FRESH_DAYS = 7


@dataclass(frozen=True, slots=True)
class SiteDiscovery:
    """Resultado da descoberta de um site em uma rodada.

    Attributes:
        record: Contagens e desfecho (é o que fica gravado por rodada).
        new: As URLs novas, prontas para a triagem.
    """

    record: SiteRunRecord
    new: tuple[DiscoveredUrl, ...]


@dataclass(frozen=True, slots=True)
class RunResult:
    """Resultado de uma rodada completa.

    Attributes:
        run_id: Identificador da rodada no estado.
        records: Um registro por site, na ordem de entrada.
        new_urls: Todas as URLs novas, agrupadas por site na ordem de entrada.
    """

    run_id: int
    records: tuple[SiteRunRecord, ...]
    new_urls: tuple[DiscoveredUrl, ...]


def discover_site(
    site: Site,
    fetcher: Fetcher,
    store: StateStore,
    *,
    run_id: int,
    now: datetime,
    user_agent: str,
    max_sitemap_fetches: int = DEFAULT_MAX_SITEMAP_FETCHES,
    fresh_days: int = DEFAULT_FRESH_DAYS,
    corpus: CorpusIndex | None = None,
    off_topic_segments: frozenset[str] = frozenset(),
    l1: L1Filter | None = None,
) -> SiteDiscovery:
    """Descobre as URLs novas de um site e atualiza o estado.

    Toda URL inédita para o estado recebe uma classificação, gravada junto:

    - ``baseline``: a primeira rodada bem-sucedida do site; é histórico, não conta como novo.
    - ``backlog``: inédita para nós, mas com data mais antiga que ``fresh_days`` (arquivo que só
      agora apareceu em um sitemap).
    - ``filtered``: descartada pelo L0 (forma da URL: listagem, seção, mídia...) ou, se ``l1`` foi
      informado, pelo L1 (manchete com pouco sinal de notícia adversa).
    - ``known``: já está no corpus de Mídias IA (conferindo o hash atual e o legado).
    - ``new``: o resto — candidata à triagem.

    Sitemaps filhos cujo ``lastmod`` não mudou desde a última leitura são dispensados.

    Args:
        site: Fonte a explorar.
        fetcher: Porta HTTP.
        store: Estado persistente.
        run_id: Rodada em curso.
        now: Instante de referência (gravado como "primeira vez visto").
        user_agent: Nome do nosso agente (regras do robots.txt).
        max_sitemap_fetches: Orçamento de sitemaps requisitados.
        fresh_days: Idade máxima, em dias, para uma URL inédita contar como nova.
        corpus: Índice do corpus; se ausente, nada é marcado como ``known``.
        off_topic_segments: Seções de temas sem interesse a descartar no L0 (vazio: nenhuma).
        l1: Filtro por manchete; se ausente, só o L0 é aplicado.

    Returns:
        O registro da rodada para o site e as URLs novas.
    """
    exploration = explore_site(
        site,
        fetcher,
        user_agent=user_agent,
        max_sitemap_fetches=max_sitemap_fetches,
        skip_child=lambda ref: _unchanged(store, ref),
    )
    if exploration.status is not ProbeStatus.OK:
        return SiteDiscovery(_failure_record(site, exploration), ())

    found = _collect(site, exploration)
    already = store.seen_hashes(item.url_hash for item in found)
    unseen = [item for item in found if item.url_hash not in already]

    first_run = not store.site_has_baseline(site.key)
    baseline: list[DiscoveredUrl] = []
    backlog: list[DiscoveredUrl] = []
    known: list[DiscoveredUrl] = []
    filtered: list[DiscoveredUrl] = []
    reasons: dict[str, str] = {}
    new: list[DiscoveredUrl] = []
    if first_run:
        baseline = unseen
    else:
        cutoff = timedelta(days=fresh_days)
        backlog = [u for u in unseen if u.timestamp is not None and now - u.timestamp > cutoff]
        backlog_hashes = {u.url_hash for u in backlog}
        fresh = [u for u in unseen if u.url_hash not in backlog_hashes]
        for candidate in fresh:
            reason = l0_filter(candidate.url, off_topic_segments=off_topic_segments)
            if reason is None and l1 is not None:
                reason = l1.check(language=site.language, url=candidate.url, title=candidate.title)
            if reason is not None:
                filtered.append(candidate)
                reasons[candidate.url_hash] = reason.value
        filtered_hashes = set(reasons)
        candidates = [u for u in fresh if u.url_hash not in filtered_hashes]
        in_corpus = (
            find_known_urls([u.url for u in candidates], corpus) if corpus and candidates else set()
        )
        known = [u for u in candidates if u.url in in_corpus]
        new = [u for u in candidates if u.url not in in_corpus]

    for kind, group in (
        (UrlKind.BASELINE, baseline),
        (UrlKind.BACKLOG, backlog),
        (UrlKind.KNOWN, known),
        (UrlKind.FILTERED, filtered),
        (UrlKind.NEW, new),
    ):
        store.record_urls(group, run_id=run_id, now=now, kind=kind, reasons=reasons)
    if first_run:
        store.mark_baseline(site.key, run_id)
    for item in exploration.sitemaps:
        store.record_sitemap(item.url, site.key, item.declared_lastmod, now)

    record = SiteRunRecord(
        site_key=site.key,
        status=ProbeStatus.OK.value,
        new_urls=len(new),
        baseline_urls=len(baseline),
        backlog_urls=len(backlog),
        seen_urls=len(found),
        sitemaps_fetched=exploration.fetched,
        sitemaps_skipped=exploration.skipped,
        detail=exploration.failure_detail,
        known_urls=len(known),
        sitemaps_failed=exploration.failed,
        filtered_urls=len(filtered),
    )
    return SiteDiscovery(record, tuple(new))


def discover_all(
    sites: Sequence[Site],
    fetcher: Fetcher,
    store: StateStore,
    *,
    user_agent: str,
    workers: int = 8,
    max_sitemap_fetches: int = DEFAULT_MAX_SITEMAP_FETCHES,
    fresh_days: int = DEFAULT_FRESH_DAYS,
    corpus: CorpusIndex | None = None,
    off_topic_segments: frozenset[str] = frozenset(),
    l1: L1Filter | None = None,
    clock: Callable[[], datetime] | None = None,
    on_progress: Callable[[int, int, SiteRunRecord], None] | None = None,
) -> RunResult:
    """Executa uma rodada de descoberta em vários sites, em paralelo.

    Uma falha inesperada em um site vira um registro ``error`` para ele e não interrompe os
    demais. O intervalo por host é responsabilidade do ``fetcher``.

    Args:
        sites: Fontes a explorar.
        fetcher: Porta HTTP compartilhada entre as threads (deve ser thread-safe).
        store: Estado persistente.
        user_agent: Nome do nosso agente.
        workers: Número de threads.
        max_sitemap_fetches: Orçamento de sitemaps por site.
        fresh_days: Janela de frescor das URLs novas.
        corpus: Índice do corpus (as consultas são serializadas entre as threads).
        off_topic_segments: Seções de temas sem interesse a descartar no L0 (vazio: nenhuma).
        l1: Filtro por manchete (opcional).
        clock: Relógio (injetável); padrão é o horário atual em UTC.
        on_progress: Chamada a cada site concluído, na thread que chamou ``discover_all``.

    Returns:
        O resultado da rodada.
    """
    now_fn = clock or (lambda: datetime.now(UTC))
    started = now_fn()
    run_id = store.start_run(started)
    shared_corpus = SynchronizedCorpusIndex(corpus) if corpus is not None else None

    def work(site: Site) -> SiteDiscovery:
        try:
            return discover_site(
                site,
                fetcher,
                store,
                run_id=run_id,
                now=started,
                user_agent=user_agent,
                max_sitemap_fetches=max_sitemap_fetches,
                fresh_days=fresh_days,
                corpus=shared_corpus,
                off_topic_segments=off_topic_segments,
                l1=l1,
            )
        except Exception as error:  # noqa: BLE001 - isola o site que falhou
            detail = f"{type(error).__name__}: {error}"
            record = SiteRunRecord(site.key, ProbeStatus.ERROR.value, 0, 0, 0, 0, 0, 0, detail)
            return SiteDiscovery(record, ())

    results: list[SiteDiscovery | None] = [None] * len(sites)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, site): index for index, site in enumerate(sites)}
        for done, future in enumerate(as_completed(futures), start=1):
            discovery = future.result()
            results[futures[future]] = discovery
            store.record_site_run(run_id, discovery.record)
            if on_progress is not None:
                on_progress(done, len(sites), discovery.record)

    store.finish_run(run_id, now_fn())
    ordered = [item for item in results if item is not None]
    return RunResult(
        run_id=run_id,
        records=tuple(item.record for item in ordered),
        new_urls=tuple(url for item in ordered for url in item.new),
    )


def _unchanged(store: StateStore, ref: SitemapRef) -> bool:
    """Um sitemap filho não mudou se o índice declara o mesmo ``lastmod`` da última leitura."""
    return ref.lastmod is not None and store.sitemap_lastmod(ref.loc) == ref.lastmod


def _collect(site: Site, exploration: Exploration) -> list[DiscoveredUrl]:
    """URLs da fonte nos sitemaps lidos, sem repetição (variantes de uma URL contam uma vez)."""
    unique: dict[str, DiscoveredUrl] = {}
    for item in exploration.sitemaps:
        for entry in item.sitemap.entries:
            if site.owns(entry.loc):
                found = DiscoveredUrl.from_entry(entry, site_key=site.key, source_sitemap=item.url)
                unique.setdefault(found.url_hash, found)
    return list(unique.values())


def _failure_record(site: Site, exploration: Exploration) -> SiteRunRecord:
    """Registro de um site cuja exploração não produziu sitemaps utilizáveis."""
    return SiteRunRecord(
        site_key=site.key,
        status=exploration.status.value,
        new_urls=0,
        baseline_urls=0,
        backlog_urls=0,
        seen_urls=0,
        sitemaps_fetched=exploration.fetched,
        sitemaps_skipped=exploration.skipped,
        detail=exploration.detail,
    )
