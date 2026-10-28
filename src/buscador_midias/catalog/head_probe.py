"""Decide, só pelo começo de um documento, se ele inteiro está fora da janela.

O leitor entrega o prefixo já recebido (sem esperar o resto). O prefixo é fechado na última
entrada completa, interpretado como qualquer documento e só corta se, além do topo estar fora da
janela, as primeiras datas do próprio prefixo ainda estiverem em ordem decrescente: a ordem
registrada no link é conferida a cada leitura, nunca apenas presumida.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.read_order import JITTER, NEWEST_FIRST, dated_sequence
from buscador_midias.ingestion.flow import head_outside

# Menos datas que isso não bastam para confiar no topo nem na ordem do prefixo.
MIN_DATED = 3
ENTRY_CLOSERS = (b"</item>", b"</entry>", b"</url>", b"</sitemap>")
ROOTS = (
    (b"<rss", b"</channel></rss>"),
    (b"<feed", b"</feed>"),
    (b"<rdf:RDF", b"</rdf:RDF>"),
    (b"<urlset", b"</urlset>"),
    (b"<sitemapindex", b"</sitemapindex>"),
)


def _closed(prefix: bytes) -> bytes | None:
    """Prefixo fechado na última entrada completa, ou ``None`` se não for feed nem sitemap."""
    head = prefix[:4096]
    root = next(
        (tail for open_tag, tail in ROOTS if open_tag in head),
        None,
    )
    if root is None:
        return None
    if prefix.rstrip().endswith(root.split(b"</channel>")[-1]):
        return prefix
    cut = max(
        prefix.rfind(closer) + len(closer) if closer in prefix else -1 for closer in ENTRY_CLOSERS
    )
    return None if cut < 0 else prefix[:cut] + root


def prefix_outside(prefix: bytes, since: datetime) -> dict[str, Any] | None:
    """``head_outside`` aplicado ao começo do documento, ou ``None`` se não dá para cortar."""
    if prefix.startswith(b"\x1f\x8b"):
        return None
    body = _closed(prefix)
    if body is None:
        return None
    try:
        document = parse_document(body, "https://prefixo.invalid/")
    except Exception:  # noqa: BLE001 - prefixo ilegível nunca corta, só segue a leitura completa
        return None
    dated = [m for m in dated_sequence(document) if m]
    if len(dated) < MIN_DATED:
        return None
    if not all(a + JITTER >= b for a, b in zip(dated, dated[1:], strict=False)):
        return None
    return head_outside(NEWEST_FIRST, dated[0].isoformat(), since)
