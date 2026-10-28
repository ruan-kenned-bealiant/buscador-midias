"""Leitura do principal/reserva e dos complementos, com deduplicação canônica."""

from collections.abc import Sequence
from dataclasses import replace

from buscador_midias.discovery.explore import ExploredSitemap, ProbeStatus
from buscador_midias.discovery.sources import (
    CascadeResult,
    DiscoverySource,
    SkipChild,
    SourceFind,
    SourceKind,
    run_cascade,
)
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.domain.site import Site
from buscador_midias.net.budget import BudgetFetcher


class StrategySetSource:
    """Tenta a reserva se necessário e sempre confere os complementos selecionados."""

    kind = SourceKind.COMBINED

    def __init__(
        self,
        main: Sequence[DiscoverySource],
        complements: Sequence[DiscoverySource],
        budget: BudgetFetcher,
        *,
        omitted: int = 0,
    ) -> None:
        """Recebe mecanismos limitados e o orçamento compartilhado com o fallback."""
        self.main = tuple(main)
        self.complements = tuple(complements)
        self.budget = budget
        self.omitted = omitted

    def applies(self, previous: Sequence[SourceFind]) -> bool:
        """O conjunto validado precede a cascata genérica."""
        return True

    def find(self, site: Site, *, skip_child: SkipChild | None = None) -> SourceFind:
        """Une resultados disponíveis e explicita falhas ou mecanismos não lidos."""
        baseline = run_cascade(site, self.main, skip_child=skip_child)
        attempts = list(baseline.attempts)
        unattempted = self.omitted
        for position, source in enumerate(self.complements):
            if self.budget.exhausted:
                unattempted += len(self.complements) - position
                break
            attempts.append(source.find(site, skip_child=skip_child))
        successful = [r for r in attempts if r.ok]
        failures = [r for r in attempts if not r.ok]
        details = [r.detail for r in failures if r.detail]
        details += [r.failure_detail for r in successful if r.failure_detail]
        if unattempted:
            details.append(f"limite de complementos/orçamento: {unattempted} mecanismos não lidos")
        detail = "; ".join(dict.fromkeys(details))
        fetched = sum(r.fetched for r in attempts)
        skipped = sum(r.skipped for r in attempts)
        failed = len(failures) + sum(r.failed for r in attempts) + unattempted
        if not successful:
            status = CascadeResult(None, tuple(attempts)).status
            kind = next((r.kind for r in attempts if r.status is status), SourceKind.FEED)
            if any(
                r.kind is SourceKind.SITEMAP and r.status is ProbeStatus.BLOCKED for r in attempts
            ):
                kind = SourceKind.SITEMAP
            return SourceFind(
                kind,
                status,
                detail or "nenhum mecanismo elegível",
                (),
                fetched=fetched,
                skipped=skipped,
                failed=failed,
            )
        urls: dict[str, DiscoveredUrl] = {}
        sitemaps: dict[str, ExploredSitemap] = {}
        for result in successful:
            for item in result.urls:
                previous = urls.get(item.url_hash)
                if previous is None:
                    urls[item.url_hash] = item
                else:
                    preferred, other = (
                        (item, previous)
                        if item.publication_date and not previous.publication_date
                        else (previous, item)
                    )
                    urls[item.url_hash] = replace(
                        preferred,
                        title=preferred.title or other.title,
                        lastmod=preferred.lastmod or other.lastmod,
                    )
            for document in result.sitemaps:
                previous_map = sitemaps.get(document.url)
                if previous_map is None or (
                    previous_map.declared_lastmod is None and document.declared_lastmod is not None
                ):
                    sitemaps[document.url] = document
        return SourceFind(
            SourceKind.COMBINED if len(successful) > 1 else successful[0].kind,
            ProbeStatus.OK,
            "",
            tuple(urls.values()),
            tuple(sitemaps.values()),
            fetched=fetched,
            skipped=skipped,
            failed=failed,
            failure_detail=detail,
        )
