"""Consulta de RSS/sitemaps funcionais e contagens com escopo explícito."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from lxml import etree

from buscador_midias.discovery.feeds import parse_feed_entries
from buscador_midias.discovery.sitemap_parser import parse_sitemap


def count_xml(body: bytes, url: str) -> dict[str, Any]:
    """Conta URLs sem avaliar datas; filhos de índice não são notícias."""
    if body.startswith((b"\x1f\x8b", b"PK")):
        parsed = parse_sitemap(body)
        return dict(
            kind="sitemap",
            listed=len({e.loc for e in parsed.entries}),
            children=len({e.loc for e in parsed.children}),
            index=bool(parsed.children) or parsed.kind.value == "index",
        )
    tree: Any = etree.fromstring(
        body.lstrip(b"\xef\xbb\xbf \t\r\n"),
        etree.XMLParser(resolve_entities=False, no_network=True),
    )
    name = etree.QName(tree).localname.lower()
    if name in ("rss", "feed", "rdf"):
        return dict(
            kind="feed",
            listed=len({e.url for e in parse_feed_entries(body, url)}),
            children=0,
            index=False,
        )
    if name == "urlset":
        urls = tree.xpath('./*[local-name()="url"]/*[local-name()="loc"]/text()')
        return dict(
            kind="sitemap",
            listed=len({str(u).strip() for u in urls if str(u).strip()}),
            children=0,
            index=False,
        )
    if name == "sitemapindex":
        refs = tree.xpath('./*[local-name()="sitemap"]/*[local-name()="loc"]/text()')
        return dict(
            kind="sitemap",
            listed=0,
            children=len({str(u).strip() for u in refs if str(u).strip()}),
            index=True,
        )
    raise ValueError("Conteúdo salvo não é RSS/sitemap")


class FunctionalView:
    """Retrato local somente leitura, sem busca ou conexão ao banco."""

    def __init__(self, directory: Path) -> None:
        """Define a pasta do retrato gerado offline."""
        self.directory = directory
        self.lock = threading.RLock()
        self.stamp = -1
        self.data: dict[str, Any] = {}

    def _refresh(self) -> None:
        file = self.directory / "retrato.json"
        stamp = file.stat().st_mtime_ns
        if stamp != self.stamp:
            self.data = json.loads(file.read_text(encoding="utf-8"))
            self.stamp = stamp

    def summary(self) -> dict[str, Any]:
        """Mostra todas as fontes, incluindo as sem mecanismo funcional comprovado."""
        with self.lock:
            self._refresh()
            return dict(meta=self.data["meta"], sources=self.data["sources"])

    def mechanisms(
        self, site: str, kind: str, search: str, offset: int, limit: int
    ) -> dict[str, Any]:
        """Pagina mecanismos técnicos, sem filtrar por data ou uso frequente/periódico."""
        with self.lock:
            self._refresh()
            source = next((s for s in self.data["sources"] if s["key"] == site), None)
            if source is None:
                raise KeyError(site)
            search = search.casefold()
            rows = [
                r
                for r in self.data["mechanisms"].get(site, [])
                if (kind == "all" or r["kind"] == kind)
                and (not search or search in r["url"].casefold())
            ]
            return dict(
                source=source,
                total=len(rows),
                offset=offset,
                limit=limit,
                items=rows[offset : offset + limit],
            )
