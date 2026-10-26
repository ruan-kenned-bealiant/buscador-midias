#!/usr/bin/env python3
"""Amostra finita de acesso público: não altera o catálogo nem inicia ingestão/IA."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from buscador_midias.catalog.http_session import HttpSessions
from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.reader import TestLimitError, TestReader

DEFAULT_URLS = [
    "https://neofeed.com.br/noticias-sobre/entrevistas/feed/",
    "https://www.poder360.com.br/tag/news-feed/feed/",
    "https://especial.correiobraziliense.com.br/feed/rss2",
    "https://g1.globo.com/rss/g1/mg/sul-de-minas/",
    "https://www.brasil247.com/esporte/feed/",
    "https://www1.folha.uol.com.br/comida/sitemap.xml",
]


def probe(url: str, sessions: HttpSessions) -> dict[str, Any]:
    """Duas leituras para observar sessão; até três requisições físicas no total por URL."""
    samples: list[dict[str, Any]] = []
    requests = 0
    for _ in range(2):
        if requests >= 3:
            break
        reader = TestReader(
            [url], lambda _: None, seconds=15, requests=3 - requests, sessions=sessions
        )
        start = time.monotonic()
        sample: dict[str, Any] = dict(url=url)
        try:
            response = reader.get(url)
            sample.update(
                status=response.status,
                http_ok=response.ok,
                error=response.error,
                final_url=response.final_url,
                bytes=len(response.content),
                retry_after_seconds=response.retry_after_seconds,
                xml_valid=False,
            )
            if response.ok:
                try:
                    doc = parse_document(
                        response.content, response.final_url or url, 8 * 1024 * 1024
                    )
                    sample.update(
                        xml_valid=True,
                        kind=doc["kind"],
                        entries=len(doc["entries"]),
                        children=len(doc["children"]),
                    )
                except ValueError as error:
                    sample["xml_error"] = str(error)[:200]
        except TestLimitError as error:
            sample.update(http_ok=False, error=str(error))
        requests += reader.requests
        sample.update(
            seconds=round(time.monotonic() - start, 3),
            requests=reader.requests,
            connections_reused=reader.connections_reused,
            cookies_sent=reader.cookies_sent,
        )
        samples.append(sample)
        if sample.get("error") or not sample.get("http_ok"):
            break
    return dict(url=url, samples=samples, physical_requests=requests)


def main() -> None:
    """Persiste somente metadados e resultados; não grava cookies nem corpos."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*")
    args = parser.parse_args()
    sessions = HttpSessions()
    try:
        results = [probe(url, sessions) for url in (args.urls or DEFAULT_URLS)]
    finally:
        sessions.close()
    stamp = datetime.now(UTC)
    root = Path("var/gerenciador/http-sessoes")
    root.mkdir(parents=True, exist_ok=True)
    path = root / (stamp.strftime("%Y%m%dT%H%M%S") + ".json")
    path.write_text(
        json.dumps(
            dict(observed_at=stamp.isoformat(), results=results), ensure_ascii=False, indent=2
        )
    )
    print(json.dumps(dict(report=str(path), results=results), ensure_ascii=False))


if __name__ == "__main__":
    main()
