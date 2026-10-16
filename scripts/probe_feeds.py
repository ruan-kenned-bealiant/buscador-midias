"""Mede quantas fontes oferecem feed RSS/Atom ou API do WordPress, como alternativa ao sitemap.

Uso (leve: algumas requisições por fonte, com ``robots.txt`` e intervalo por host; NÃO usa o
Bright Data, então um site que bloqueia o nosso agente aparece como bloqueado):
    python scripts/probe_feeds.py [--control-ok 60] [--seed 0] [--workers 8] [--interval 1.0]
                                  [--limit N] [--out CSV]

Parte dos status da sondagem de sitemaps (``var/probe-*.csv``) e olha as fontes que o sitemap não
atende de graça (``blocked``, ``no_sitemap``, ``error``, ``robots_disallowed``), mais uma amostra de
controle de fontes ``ok`` para comparar. Para cada fonte:

1. baixa a home e procura ``<link rel="alternate">`` de feed e o ``<link>`` da API do WordPress;
2. se a home não declara feed, tenta ``/feed/``, ``/rss.xml`` e ``/rss``;
3. lê o feed (itens e data mais recente) e testa ``wp/v2/posts`` quando há API do WordPress.

Um feed "serve" quando tem itens e o mais novo tem até 72 h. Escreve um CSV por fonte e imprime um
resumo por grupo.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from buscador_midias.discovery.feeds import find_feed_links, find_wordpress_api, parse_feed
from buscador_midias.domain.site import Site
from buscador_midias.extraction.fetch import ArticleFetcher, PageStatus
from buscador_midias.net.fetcher import USER_AGENT
from buscador_midias.net.stack import build_fetcher
from buscador_midias.sites.loader import load_sites

ROOT = Path(__file__).resolve().parent.parent
PROBE_FILES = ("probe-completo.csv", "probe-reprobe.csv", "probe-reprobe2.csv", "probe-bd.csv")
PROBLEM_STATUSES = ("blocked", "no_sitemap", "error", "robots_disallowed")
FRESH_HOURS = 72.0
GUESSED_PATHS = ("/feed/", "/rss.xml", "/rss")
MAX_DECLARED = 3
TIMEOUT_SECONDS = 20.0


@dataclass(slots=True)
class Row:
    """Resultado da medição de uma fonte."""

    site_key: str
    group: str
    home: str = ""
    declared_feeds: int = 0
    feed_origin: str = "-"  # declared | guessed | -
    feed_url: str = ""
    feed_kind: str = ""
    items: int = 0
    newest_age_h: float | None = None
    wp_api: bool = False
    wp_posts: int = 0
    wp_newest_age_h: float | None = None

    @property
    def feed_alive(self) -> bool:
        """Feed com itens e o mais novo dentro da janela."""
        return self.items > 0 and self.newest_age_h is not None and self.newest_age_h <= FRESH_HOURS

    @property
    def feed_undated(self) -> bool:
        """Feed com itens, mas sem data legível (não dá para saber se está vivo)."""
        return self.items > 0 and self.newest_age_h is None

    @property
    def wp_alive(self) -> bool:
        """API do WordPress com post dentro da janela."""
        return (
            self.wp_posts > 0
            and self.wp_newest_age_h is not None
            and self.wp_newest_age_h <= FRESH_HOURS
        )

    @property
    def any_alive(self) -> bool:
        """Alguma alternativa gratuita ao sitemap serve."""
        return self.feed_alive or self.wp_alive


def consolidated_status() -> dict[str, str]:
    """Último status de cada fonte nas sondagens de sitemap (as re-sondagens vencem)."""
    status: dict[str, str] = {}
    for name in PROBE_FILES:
        path = ROOT / "var" / name
        if not path.is_file():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for record in csv.DictReader(handle):
                status[record["site_key"]] = record["status"]
    return status


def age_hours(moment: datetime | None, now: datetime) -> float | None:
    """Idade em horas (nunca negativa); ``None`` sem data."""
    return None if moment is None else max(0.0, (now - moment).total_seconds() / 3600)


def probe_site(site: Site, group: str, articles: ArticleFetcher, now: datetime) -> Row:
    """Mede uma fonte: home, feed e API do WordPress."""
    row = Row(site.key, group)
    home = articles.fetch(site.url)
    row.home = home.status.value
    base = home.final_url or site.url
    declared: tuple[str, ...] = ()
    wp_api: str | None = None
    if home.status is PageStatus.OK:
        declared = find_feed_links(home.content, base)
        wp_api = find_wordpress_api(home.content, base)
    row.declared_feeds = len(declared)
    row.wp_api = wp_api is not None

    origin = "{0.scheme}://{0.netloc}".format(urlsplit(base))
    candidates = [(url, "declared") for url in declared[:MAX_DECLARED]] or [
        (origin + path, "guessed") for path in GUESSED_PATHS
    ]
    for url, how in candidates:
        page = articles.fetch(url)
        summary = parse_feed(page.content) if page.status is PageStatus.OK else None
        if summary is None:
            continue
        age = age_hours(summary.newest, now)
        if row.items == 0 or summary.items > row.items:
            row.feed_origin, row.feed_url, row.feed_kind = how, url, summary.kind
            row.items, row.newest_age_h = summary.items, age
        if row.feed_alive:
            break
    if wp_api:
        _probe_wordpress(row, wp_api, articles, now)
    return row


def _probe_wordpress(row: Row, api: str, articles: ArticleFetcher, now: datetime) -> None:
    """Pede os 5 posts mais recentes à API REST e guarda quantos vieram e a idade do mais novo."""
    url = f"{api.rstrip('/')}/wp/v2/posts?per_page=5&_fields=date_gmt"
    page = articles.fetch(url)
    if page.status is not PageStatus.OK:
        return
    try:
        posts = json.loads(page.content)
    except ValueError:
        return
    if not isinstance(posts, list):
        return
    dates = []
    for post in posts:
        raw = post.get("date_gmt") if isinstance(post, dict) else None
        try:
            dates.append(datetime.fromisoformat(str(raw)).replace(tzinfo=UTC))
        except ValueError:
            continue
    row.wp_posts = len(posts)
    row.wp_newest_age_h = age_hours(max(dates), now) if dates else None


def summarize(rows: list[Row]) -> str:
    """Resumo por grupo, em texto."""
    by_group: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_group[row.group].append(row)
    lines = [
        f"{'grupo':<18}{'fontes':>7}{'home ok':>9}{'feed decl.':>11}{'feed serve':>11}"
        f"{'WP API':>8}{'WP serve':>9}{'algum serve':>12}"
    ]

    def pct(count: int, total: int) -> str:
        return f"{count} ({count / total:.0%})" if total else "-"

    ordered = [*PROBLEM_STATUSES, "ok"]
    for group in sorted(by_group, key=lambda g: ordered.index(g) if g in ordered else 99):
        rs = by_group[group]
        n = len(rs)
        lines.append(
            f"{group:<18}{n:>7}"
            f"{pct(sum(r.home == 'ok' for r in rs), n):>9}"
            f"{pct(sum(r.declared_feeds > 0 for r in rs), n):>11}"
            f"{pct(sum(r.feed_alive for r in rs), n):>11}"
            f"{pct(sum(r.wp_api for r in rs), n):>8}"
            f"{pct(sum(r.wp_alive for r in rs), n):>9}"
            f"{pct(sum(r.any_alive for r in rs), n):>12}"
        )
    problem = [r for r in rows if r.group in PROBLEM_STATUSES]
    if problem:
        lines.append("")
        lines.append(
            f"fontes sem sitemap gratuito: {len(problem)} | com alternativa que serve: "
            f"{sum(r.any_alive for r in problem)} | feed sem data legível: "
            f"{sum(r.feed_undated for r in problem)}"
        )
    alive = [r for r in rows if r.feed_alive]
    if alive:
        median_items = statistics.median(r.items for r in alive)
        median_age = statistics.median(r.newest_age_h or 0 for r in alive)
        lines.append(
            f"feeds que servem ({len(alive)}): mediana de {median_items:.0f} itens "
            f"e item mais novo com {median_age:.1f} h"
        )
        guessed = sum(r.feed_origin == "guessed" for r in alive)
        lines.append(f"  achados só por caminho adivinhado (/feed/, /rss.xml, /rss): {guessed}")
    return "\n".join(lines)


def build_sample(
    sites: dict[str, Site], status: dict[str, str], control_ok: int, seed: int, limit: int | None
) -> list[tuple[Site, str]]:
    """Fontes sem sitemap gratuito mais uma amostra de controle de fontes ``ok``."""
    chosen = [
        (sites[k], s) for k, s in sorted(status.items()) if s in PROBLEM_STATUSES and k in sites
    ]
    ok_keys = sorted(k for k, s in status.items() if s == "ok" and k in sites)
    rng = random.Random(seed)
    chosen += [(sites[k], "ok") for k in rng.sample(ok_keys, min(control_ok, len(ok_keys)))]
    return chosen[:limit] if limit else chosen


def main(argv: list[str] | None = None) -> int:
    """Executa a medição e grava o CSV."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--control-ok", type=int, default=60, help="Fontes ok de controle.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--interval", type=float, default=1.0, help="Segundos entre pedidos a um host."
    )
    parser.add_argument("--limit", type=int, default=None, help="Máximo de fontes (teste rápido).")
    parser.add_argument("--out", type=Path, default=ROOT / "var" / "feeds-probe.csv")
    args = parser.parse_args(argv)

    sites = {site.key: site for site in load_sites(ROOT / "data" / "sites.csv").sites}
    sample = build_sample(sites, consolidated_status(), args.control_ok, args.seed, args.limit)
    print(f"medindo {len(sample)} fontes ({args.workers} threads)…", file=sys.stderr)
    now = datetime.now(UTC)

    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
        stack = build_fetcher(
            client=client,
            interval=args.interval,
            bright_data=False,
            env={},
            max_fallback_requests=0,
            max_bytes=5 * 1024 * 1024,
        )
        articles = ArticleFetcher(stack.fetcher, user_agent=USER_AGENT)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(lambda item: probe_site(item[0], item[1], articles, now), sample))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0])) if rows else [])
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
    print(summarize(rows))
    print(f"\nCSV: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
