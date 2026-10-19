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
        self.sections: dict[str, list[dict[str, str]]] = {}

    def refresh(self) -> None:
        """Relê somente se os arquivos de entrada tiverem mudado."""
        sm = self.directory / "auditoria-acesso-sitemaps"
        rss = self.directory / "auditoria-acesso-rss"
        extra = self.directory / "exploracao-pendentes-final"
        web = self.directory / "revisao-web-fontes"
        final = self.directory / "validacao-final"
        paths = [
            sm / "referencias.csv",
            rss / "feeds.csv",
            rss / "inventario.csv",
            sm / "resumo.json",
            rss / "resumo.json",
            extra / "acesso.csv",
            extra / "fontes.csv",
            extra / "resumo.json",
            web / "acesso.csv",
            web / "fontes.csv",
            web / "resumo.json",
            final / "acesso.csv",
            final / "fontes.csv",
            final / "resumo.json",
            final / "secoes.csv",
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
        for row in _csv(paths[8]):
            self._add(records, row, row["kind"], row.get("attempted") == "True")
        validation = _json(paths[13])
        final_sources = {row["site_key"]: row for row in _csv(paths[12])}
        sections: dict[str, list[dict[str, str]]] = defaultdict(list)
        for section in _csv(paths[14]):
            site = self.sites.get(section["site_key"])
            if site is not None and safe_site_url(site, section.get("url")):
                section["example_url"] = safe_site_url(site, section.get("example_url")) or ""
                sections[site.key].append(section)
        self.sections = dict(sections)
        if validation.get("closed") and paths[11].is_file():
            # Retrato atual completo, inclusive pendências: não mascarar falhas novas
            # com uma amostra bem-sucedida de uma rodada anterior.
            records = defaultdict(list)
            for row in _csv(paths[11]):
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
        web_sources = {row["site_key"]: row for row in _csv(paths[9])}
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
                "explored": site.key in exploration and not validation.get("closed"),
                "exploration_status": exploration.get(site.key, {}).get("status"),
                "exploration_detail": exploration.get(site.key, {}).get("detail"),
                "web_review": web_sources.get(site.key),
                "final_validation": final_sources.get(site.key),
            }
        summaries = [_json(paths[3]), _json(paths[4])]
        self.summary = {
            "available": any(paths[i].is_file() for i in (0, 1, 2, 5, 6, 8, 9))
            or (bool(validation.get("closed")) and paths[11].is_file()),
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
                (folder / "resultado.xlsx").is_file() for folder in (rss, extra, web, final)
            ),
            "exploration": _json(paths[7]),
            "web_review": _json(paths[10]),
            "final_validation": validation,
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
        samples = []
        for observation in json.loads(row.get("samples") or "[]")[:3]:
            candidate = safe_site_url(site, observation.get("url"))
            if candidate and not is_excluded(candidate, exclude):
                samples.append(
                    {
                        "url": candidate,
                        "title": str(observation.get("title") or "Página testada")[:500],
                        "http_status": observation.get("http_status"),
                        "robots_http_status": observation.get("robots_http_status"),
                        "accessible": observation.get("accessible") is True,
                        "readable": observation.get("readable") is True,
                        "reason": str(observation.get("reason") or "")[:500],
                        "text_chars": observation.get("text_chars"),
                    }
                )
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
                "samples": samples,
                "xml_http": row.get("http_status"),
                "basis": row.get("basis"),
                "found_on": (
                    row.get("found_on")
                    if safe_mechanism_url(site, row.get("found_on") or "")
                    else None
                ),
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
        elif mode == "partial":
            rows = [r for r in rows if r["partial"] or not r["attempted"]]
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

    def sections_page(
        self, key: str, *, mode: str, search: str, offset: int, limit: int
    ) -> dict[str, Any]:
        """Lista correspondências editoriais desta rodada, sem extrapolar a amostra."""
        rows = self.sections.get(key, [])
        if mode == "confirmed":
            rows = [r for r in rows if r.get("accessible") == "True"]
        elif mode == "unconfirmed":
            rows = [r for r in rows if r.get("accessible") != "True"]
        query = search.casefold()
        rows = [r for r in rows if query in (r.get("title", "") + " " + r["url"]).casefold()]
        return {
            "total": len(rows),
            "sections": rows[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "summary": self.summary.get("final_validation", {}),
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
