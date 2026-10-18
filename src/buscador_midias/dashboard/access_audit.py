"""Retrato local das auditorias de acesso, sem rede ou gravações no banco."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from buscador_midias.dashboard.mapping import safe_site_url
from buscador_midias.domain.site import Site
from buscador_midias.strategy.manual import ManualRulesBook, is_excluded
from buscador_midias.strategy.mechanism_inventory import safe_mechanism_url


class AccessAudit:
    """Recarrega os relatórios quando mudam; preserva não testados como desconhecidos."""

    def __init__(self, directory: Path, sites: list[Site], rules: ManualRulesBook) -> None:
        """Define as fontes e a pasta dos relatórios locais."""
        self.directory = directory
        self.sites = {s.key: s for s in sites}
        self.rules = rules
        self.signature: tuple[Any, ...] | None = None
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.summary: dict[str, Any] = {}

    def refresh(self) -> None:
        """Relê somente se os arquivos de entrada tiverem mudado."""
        sm = self.directory / "auditoria-acesso-sitemaps"
        rss = self.directory / "auditoria-acesso-rss"
        extra = self.directory / "exploracao-pendentes-final"
        paths = [
            sm / "referencias.csv",
            rss / "feeds.csv",
            rss / "inventario.csv",
            sm / "resumo.json",
            rss / "resumo.json",
            extra / "acesso.csv",
            extra / "fontes.csv",
            extra / "resumo.json",
        ]
        signature = tuple(
            (p.stat().st_mtime_ns, p.stat().st_size) if p.is_file() else None for p in paths
        )
        if signature == self.signature:
            return
        records: dict[str, list[dict[str, Any]]] = defaultdict(list)
        rss_tested = {(r["site_key"], r["feed"]): r for r in _csv(paths[1])}
        for row in _csv(paths[0]):
            self._add(records, row, "sitemap", True)
        for inventory in _csv(paths[2]):
            key = (inventory["site_key"], inventory["feed"])
            row = {**inventory, **rss_tested.get(key, {})}
            self._add(records, row, "feed", row.get("tentado") == "True")
        # Uma exportação parcial ainda pode ter os feeds verificados sem o inventário.
        if not paths[2].is_file():
            for row in rss_tested.values():
                self._add(records, row, "feed", row.get("tentado") == "True")
        for row in _csv(paths[5]):
            self._add(records, row, row["kind"], row.get("attempted") == "True")
        for source_key, rows in records.items():
            unique: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                identity = (row["url"], row["kind"])
                prior = unique.get(identity)
                # Uma nova leitura só do XML não apaga acesso a notícia já comprovado.
                if prior is None or _evidence(row) >= _evidence(prior):
                    unique[identity] = row
            records[source_key] = list(unique.values())
        exploration = {row["site_key"]: row for row in _csv(paths[6])}
        sources = {}
        for site in self.sites.values():
            rows = records.get(site.key, [])
            sources[site.key] = {
                "audited": any(r["attempted"] for r in rows),
                "accessible": any(r["accessible"] for r in rows),
                "readable": any(r["readable"] for r in rows),
                "rss_accessible": any(r["accessible"] and r["kind"] == "feed" for r in rows),
                "sitemap_accessible": any(r["accessible"] and r["kind"] == "sitemap" for r in rows),
                "mechanisms": len(rows),
                "tested": sum(r["attempted"] for r in rows),
                "untested": sum(not r["attempted"] for r in rows),
                "accessible_mechanisms": sum(r["accessible"] for r in rows),
                "explored": site.key in exploration,
                "exploration_status": exploration.get(site.key, {}).get("status"),
                "exploration_detail": exploration.get(site.key, {}).get("detail"),
            }
        summaries = [_json(paths[3]), _json(paths[4])]
        self.summary = {
            "available": any(paths[i].is_file() for i in (0, 1, 2, 5, 6)),
            "sources_accessible": sum(s["accessible"] for s in sources.values()),
            "sources_readable": sum(s["readable"] for s in sources.values()),
            "sources_audited": sum(s["audited"] for s in sources.values()),
            "rss_sources_accessible": sum(s["rss_accessible"] for s in sources.values()),
            "sitemap_sources_accessible": sum(s["sitemap_accessible"] for s in sources.values()),
            "rss_sources_readable": sum(
                any(r["readable"] and r["kind"] == "feed" for r in rows)
                for rows in records.values()
            ),
            "sitemap_sources_readable": sum(
                any(r["readable"] and r["kind"] == "sitemap" for r in rows)
                for rows in records.values()
            ),
            "sitemap_finished_at": summaries[0].get("finished_at"),
            "rss_finished_at": summaries[1].get("finished_at"),
            "rss_feeds_per_source": summaries[1].get("limits", {}).get("feeds_per_source"),
            "sitemap_max_depth": summaries[0].get("limits", {}).get("max_depth"),
            "download_available": any(
                (folder / "resultado.xlsx").is_file() for folder in (rss, extra)
            ),
            "exploration": _json(paths[7]),
        }
        self.records, self.sources, self.signature = dict(records), sources, signature

    def _add(
        self,
        records: dict[str, list[dict[str, Any]]],
        row: dict[str, str],
        kind: str,
        attempted: bool,
    ) -> None:
        site = self.sites.get(row["site_key"])
        url = row.get("root") or row.get("feed") or ""
        if site is None or not safe_mechanism_url(site, url):
            return
        exclude = self.rules.for_site(site).exclude
        if is_excluded(url, exclude):
            return
        sample = safe_site_url(site, row.get("example_url") or row.get("exemplo"))
        if sample and is_excluded(sample, exclude):
            sample = None
        accessible = attempted and bool(sample) and _true(row, "accessible", "noticia_acessivel")
        readable = accessible and _true(row, "readable", "texto_legivel")
        records[site.key].append(
            {
                "url": url,
                "kind": kind,
                "attempted": attempted,
                "status": row.get("status") or row.get("status_auditoria") or "not_tested",
                "accessible": accessible,
                "readable": readable,
                "xml_readable": _true(row, "xml_readable", "xml_legivel") if attempted else None,
                "example_url": sample,
                "example_title": row.get("example_title") or row.get("titulo_exemplo"),
                "example_http": row.get("example_http") or row.get("http_exemplo"),
                "text_chars": row.get("example_text_chars") or row.get("caracteres_exemplo"),
                "sample_attempts": row.get("sample_attempts") or row.get("tentativas_noticias"),
                "candidate_urls": row.get("candidate_urls") or row.get("links_noticias"),
                "reasons": row.get("reasons") or row.get("motivos_amostras") or "",
                "xml_errors": row.get("xml_errors") or row.get("erro_feed") or "",
                "depth_reached": row.get("depth_reached"),
                "partial": row.get("traversal_partial") == "True",
                "xml_http": row.get("http_status"),
                "basis": row.get("basis"),
            }
        )

    def page(
        self,
        key: str,
        *,
        kind: str,
        mode: str,
        search: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        """Filtra antes de paginar; os totais nunca somam fontes duplicadas."""
        rows = [r for r in self.records.get(key, []) if kind == "all" or r["kind"] == kind]
        if mode == "accessible":
            rows = [r for r in rows if r["accessible"]]
        elif mode == "readable":
            rows = [r for r in rows if r["readable"]]
        elif mode == "unconfirmed":
            rows = [r for r in rows if r["attempted"] and not r["accessible"]]
        elif mode == "untested":
            rows = [r for r in rows if not r["attempted"]]
        term = search.casefold()
        rows = [
            r
            for r in rows
            if term
            in " ".join(
                str(r[k] or "")
                for k in (
                    "url",
                    "kind",
                    "status",
                    "example_title",
                    "example_url",
                    "reasons",
                    "xml_errors",
                )
            ).casefold()
        ]
        rows.sort(
            key=lambda r: (not r["readable"], not r["accessible"], not r["attempted"], r["url"])
        )
        return {
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "records": rows[offset : offset + limit],
            "source": self.sources.get(key, {}),
            "summary": self.summary,
        }


def _csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return dict(json.loads(path.read_text()))


def _true(row: dict[str, str], *keys: str) -> bool:
    return any(row.get(k) == "True" for k in keys)


def _evidence(row: dict[str, Any]) -> tuple[bool, bool, bool, bool]:
    return row["readable"], row["accessible"], row["attempted"], bool(row["xml_readable"])
