"""Apoio dos testes de saúde: cadastro mínimo, leitores falsos e atalhos de estado."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from buscador_midias.db.catalog import CatalogRepository
from buscador_midias.db.health import HealthRepository
from buscador_midias.net.fetcher import FetchResult

ROOT = "https://jose.example"
ARTICLE = ROOT + "/noticia"


def article(published: str = "2001-01-01T12:00:00Z") -> bytes:
    data = dict(
        **{"@type": "NewsArticle"},
        headline="Reportagem sobre os acontecimentos na cidade",
        datePublished=published,
    )
    text = (
        "A reportagem ouviu moradores e autoridades sobre os acontecimentos na cidade. "
        "Os documentos apresentados detalham os impactos das medidas anunciadas. "
    )
    return (
        '<html><head><meta charset="utf-8"><title>Reportagem sobre os acontecimentos na cidade'
        '</title><script type="application/ld+json">' + json.dumps(data) + "</script></head><body>"
        "<article><h1>Reportagem sobre os acontecimentos na cidade</h1>"
        + "".join("<p>" + text + "</p>" for _ in range(15))
        + "</article></body></html>"
    ).encode()


class FakeReader:
    def __init__(self, data: dict[str, Any], progress: Any) -> None:
        self.data, self.progress, self.requests = data, progress, 0

    def get(self, url: str) -> FetchResult:
        self.requests += 1
        self.progress(dict(stage="http", message="HTTP simulado", url=url))
        response = self.data.get(url)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, FetchResult):
            return response
        return FetchResult(
            url,
            200 if response is not None else 404,
            response or b"",
            None,
            final_url=url,
            content_type="text/html" if url.endswith("/noticia") else "text/xml",
        )


def old_news(roots: list[str], progress: Any) -> FakeReader:
    """Feed com uma notícia de 2001: o teste de saúde ignora a data e vê o texto."""
    root = roots[0].rstrip("/")
    return FakeReader(
        {
            roots[1]: (
                f"<rss><channel><item><link>{root}/noticia</link>"
                "<pubDate>2001-01-01</pubDate></item></channel></rss>"
            ).encode(),
            root + "/noticia": FetchResult(
                root + "/noticia", 200, article(), None, root + "/noticia", "text/html"
            ),
        },
        progress,
    )


class FreshProbe:
    """Sonda leve com notícia de ontem: o portão de frescor libera a reativação."""

    def __call__(self, link: dict[str, Any]) -> FreshProbe:
        return self

    def get(self, url: str) -> FetchResult:
        day = (datetime.now(UTC) - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        body = (
            f"<rss><channel><item><link>{url}n</link><pubDate>{day}</pubDate></item>"
            "</channel></rss>"
        )
        return FetchResult(url, 200, body.encode(), None, url)


def build(schema: str) -> tuple[HealthRepository, CatalogRepository, dict[str, Any]]:
    """Repositórios instalados com um site (``jose.example``) e um link de feed."""
    health = HealthRepository(schema)
    health.install()
    catalog = CatalogRepository(schema)
    site = catalog.save("source", dict(name="José", url=ROOT))
    link = catalog.save(
        "endpoint", dict(url=ROOT + "/feed/", kind="feed", model="rss"), source_id=site["id"]
    )
    return health, catalog, dict(site=site, link=link)


def set_state(repo: HealthRepository, link_id: int, **columns: Any) -> None:
    """Grava colunas do estado do link (cria a linha se faltar)."""
    names = ",".join(columns)
    marks = ",".join(["%s"] * len(columns))
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in columns)
    values = [Jsonb(v) if isinstance(v, dict) else v for v in columns.values()]
    with repo.connection() as db:
        db.execute(
            f"INSERT INTO bm_link_state(link_id,{names}) VALUES(%s,{marks}) "
            f"ON CONFLICT (link_id) DO UPDATE SET {updates}",
            (link_id, *values),
        )
