"""Mede se o HTML do download direto serve de evidência: confronta com a cópia do Bright Data.

Uso (precisa de BRIGHTDATA_API_KEY e BRIGHTDATA_ZONE_UNLOCKER no ambiente; FAZ CHAMADAS PAGAS,
no máximo uma por URL amostrada):
    python scripts/eval_evidence_reuse.py [--n 30] [--per-site 3] [--seed 0] [--out CSV]
                                          [--source triage|recent]

``--source triage`` amostra URLs mantidas pela triagem; ``--source recent`` pega, de cada
fonte, URLs recentes (``lastmod``, ou a ordem do sitemap) que o L0 deixaria passar, cobrindo
fontes sem URLs ``new`` ainda.

Para cada URL da amostra baixa a matéria direto e pelo Bright Data, avalia as duas cópias
com os mesmos critérios de confiabilidade e compara o conteúdo extraído. Responde: quantas vezes o
HTML direto seria aceito, quantas dessas o Bright Data confirma (reaproveitamento seguro) e quantas
são aceitas sem confirmação (risco), além de por que o direto é rejeitado.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from buscador_midias.extraction.compare import compare_pages
from buscador_midias.extraction.fetch import ArticleFetcher, PageStatus
from buscador_midias.extraction.reliability import PageSnapshot, assess_evidence
from buscador_midias.net.fetcher import USER_AGENT
from buscador_midias.net.stack import build_fetcher, build_unlocker
from buscador_midias.triage.headline import headline_text
from buscador_midias.triage.l0 import l0_filter

ROOT = Path(__file__).resolve().parents[1]
MIN_SIMILARITY = 0.9
MIN_LENGTH_RATIO = 0.8


@dataclass(frozen=True, slots=True)
class Row:
    """Resultado de uma URL."""

    site: str
    url: str
    direct_status: str
    direct_ok: bool
    direct_reasons: tuple[str, ...]
    bd_ok: bool
    bd_reasons: tuple[str, ...]
    similarity: float | None
    same_title: bool | None
    same_date: bool | None
    length_ratio: float | None

    @property
    def agrees(self) -> bool:
        """As duas cópias são confiáveis e praticamente iguais."""
        return (
            self.direct_ok
            and self.bd_ok
            and (self.similarity or 0) >= MIN_SIMILARITY
            and (self.length_ratio or 0) >= MIN_LENGTH_RATIO
        )


def sample_urls(
    state: Path, n: int, per_site: int, rng: random.Random, source: str = "triage"
) -> list[tuple[str, str | None, str]]:
    """Amostra (url, título, fonte), até ``per_site`` por fonte.

    ``triage``: URLs mantidas pela triagem. ``recent``: as mais recentes por ``lastmod`` de cada
    fonte, para cobrir fontes que ainda não têm URLs novas.
    """
    connection = sqlite3.connect(state)
    if source == "recent":
        rows = []
        for (site,) in connection.execute("SELECT DISTINCT site_key FROM urls_seen").fetchall():
            candidates = [
                row
                for row in connection.execute(
                    "SELECT url, title, site_key FROM urls_seen WHERE site_key = ?"
                    " ORDER BY lastmod DESC NULLS LAST, rowid DESC LIMIT 40",
                    (site,),
                )
                if l0_filter(row[0]) is None  # o que o L0 descarta nunca chega à evidência
            ]
            rows += rng.sample(candidates, min(per_site, len(candidates)))
    else:
        rows = connection.execute(
            "SELECT u.url, u.title, u.site_key FROM urls_seen u"
            " JOIN url_triage t ON t.url_hash = u.url_hash AND t.decision = 'keep'"
        ).fetchall()
    rng.shuffle(rows)
    taken: Counter[str] = Counter()
    sample = []
    for url, title, site in rows:
        if taken[site] < per_site:
            taken[site] += 1
            sample.append((url, title, site))
        if len(sample) >= n:
            break
    return sample


def main(argv: list[str]) -> int:
    """Roda o experimento e imprime o resumo."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--per-site", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--source", choices=["triage", "recent"], default="triage")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--state", type=Path, default=ROOT / "var/state.db")
    parser.add_argument("--out", type=Path, default=ROOT / "var/evidence_reuse_eval.csv")
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    sample = sample_urls(args.state, args.n, args.per_site, random.Random(args.seed), args.source)
    print(
        f"{len(sample)} URLs de {len({s for *_, s in sample})} fontes; "
        f"Bright Data: até {len(sample)} chamadas"
    )
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        direct = ArticleFetcher(
            build_fetcher(
                client=client,
                interval=args.interval,
                bright_data=False,
                env={},
                max_fallback_requests=0,
                max_bytes=8 * 1024 * 1024,
            ).fetcher,
            user_agent=USER_AGENT,
        )
        unlocker = build_unlocker(client, os.environ, max_requests=len(sample))

        def evaluate(item: tuple[str, str | None, str]) -> Row:
            url, title, site = item
            expected = headline_text(url, title)
            page = direct.fetch(url)
            bd = unlocker.get(url)
            direct_ok = False
            direct_reasons: tuple[str, ...] = (f"direct_{page.status.value}",)
            if page.status is PageStatus.OK:
                snap = PageSnapshot(
                    url, page.http_status, page.content_type, page.final_url, page.content
                )
                verdict = assess_evidence(snap, expected_headline=expected, now=now)
                direct_ok, direct_reasons = verdict.ok, verdict.reasons
            bd_snap = PageSnapshot(url, bd.status, bd.content_type, bd.final_url, bd.content)
            bd_verdict = assess_evidence(bd_snap, expected_headline=expected, now=now)
            comparison = (
                compare_pages(page.content, bd.content, now=now)
                if page.status is PageStatus.OK and bd.content
                else None
            )
            return Row(
                site,
                url,
                page.status.value,
                direct_ok,
                direct_reasons,
                bd_verdict.ok,
                bd_verdict.reasons,
                comparison.text_similarity if comparison else None,
                comparison.same_title if comparison else None,
                comparison.same_published_at if comparison else None,
                comparison.length_ratio if comparison else None,
            )

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(evaluate, sample))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "fonte",
                "url",
                "direto",
                "direto_ok",
                "motivos_direto",
                "bd_ok",
                "motivos_bd",
                "similaridade",
                "mesmo_titulo",
                "mesma_data",
                "razao_tamanho",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r.site,
                    r.url,
                    r.direct_status,
                    r.direct_ok,
                    ",".join(r.direct_reasons),
                    r.bd_ok,
                    ",".join(r.bd_reasons),
                    r.similarity,
                    r.same_title,
                    r.same_date,
                    r.length_ratio,
                ]
            )

    n = len(rows)
    downloaded = sum(r.direct_status == "ok" for r in rows)
    direct_ok = [r for r in rows if r.direct_ok]
    safe = [r for r in direct_ok if r.agrees]
    risky = [r for r in direct_ok if not r.agrees]
    rejected_but_bd_ok = [r for r in rows if not r.direct_ok and r.bd_ok]
    bd_total = sum(r.bd_ok for r in rows)
    print(f"\nURLs avaliadas: {n}")
    print(f"download direto ok: {downloaded} ({downloaded / n:.0%})")
    print(f"HTML direto aceito pela avaliação: {len(direct_ok)} ({len(direct_ok) / n:.0%})")
    print(f"  confirmado pelo Bright Data (iguais): {len(safe)}")
    print(f"  aceito mas SEM confirmação (risco): {len(risky)}")
    print(f"direto rejeitado e Bright Data confiável (custo extra): {len(rejected_but_bd_ok)}")
    print(f"Bright Data confiável no total: {bd_total} ({bd_total / n:.0%})")
    reasons: Counter[str] = Counter(
        reason for r in rows if not r.direct_ok for reason in r.direct_reasons
    )
    print("motivos de rejeição do direto:", dict(reasons.most_common()))
    bd_reasons: Counter[str] = Counter(
        reason for r in rows if not r.bd_ok for reason in r.bd_reasons
    )
    print("motivos de rejeição do Bright Data:", dict(bd_reasons.most_common()))
    by_site: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_site[r.site].append(r)
    print("\npor fonte (aceitos no direto / avaliados):")
    for site, items in sorted(by_site.items()):
        print(f"  {site:28s} {sum(i.direct_ok for i in items)}/{len(items)}")
    if risky:
        print("\nACEITOS SEM CONFIRMAÇÃO (revisar):")
        for r in risky:
            print(
                f"  {r.url}\n    sim={r.similarity} razão={r.length_ratio} "
                f"título={r.same_title} bd_motivos={r.bd_reasons}"
            )
    print(f"\nCSV em {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
