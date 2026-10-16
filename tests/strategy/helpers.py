"""Apoio dos testes de estratégia: fetcher falso e construtores de sitemap, índice e feed."""

from datetime import UTC, datetime, timedelta

from buscador_midias.domain.site import Site
from buscador_midias.net.fetcher import FetchResult

UA = "BuscadorMidiasBot"
HOME = "https://exemplo.com"
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
NEWS_NS = "http://www.google.com/schemas/sitemap-news/0.9"


class FakeFetcher:
    """Responde por URL; o que não está no mapa é 404."""

    def __init__(self, responses: dict[str, bytes | FetchResult] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        response = self.responses.get(url)
        if response is None:
            return FetchResult(url=url, status=404, content=b"", error=None)
        if isinstance(response, FetchResult):
            return response
        return FetchResult(url=url, status=200, content=response, error=None)


def make_site(url: str = HOME) -> Site:
    return Site.create(
        name="Exemplo",
        url=url,
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def rfc822(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def news_sitemap(*entries: tuple[str, float], host: str = HOME) -> bytes:
    """Sitemap do Google News: (slug, horas atrás) por matéria."""
    urls = "".join(
        f"<url><loc>{host}/{slug}</loc><news:news><news:title>Título {slug}</news:title>"
        f"<news:publication_date>{iso(ago)}</news:publication_date></news:news></url>"
        for slug, ago in entries
    )
    return f'<urlset xmlns:news="{NEWS_NS}">{urls}</urlset>'.encode()


def plain_sitemap(*entries: tuple[str, float | None], host: str = HOME) -> bytes:
    """Sitemap comum: (slug, horas atrás do lastmod ou None) por matéria."""
    urls = "".join(
        f"<url><loc>{host}/{slug}</loc>"
        + (f"<lastmod>{iso(ago)}</lastmod>" if ago is not None else "")
        + "</url>"
        for slug, ago in entries
    )
    return f"<urlset>{urls}</urlset>".encode()


def sitemap_index(*children: tuple[str, float | None]) -> bytes:
    """Índice de sitemaps: (url do filho, horas atrás do lastmod ou None)."""
    body = "".join(
        f"<sitemap><loc>{loc}</loc>"
        + (f"<lastmod>{iso(ago)}</lastmod>" if ago is not None else "")
        + "</sitemap>"
        for loc, ago in children
    )
    return f"<sitemapindex>{body}</sitemapindex>".encode()


def rss(*entries: tuple[str, float | None], host: str = HOME) -> bytes:
    """Feed RSS: (slug, horas atrás ou None) por item."""
    items = "".join(
        f"<item><title>T {slug}</title><link>{host}/{slug}</link>"
        + (f"<pubDate>{rfc822(ago)}</pubDate>" if ago is not None else "")
        + "</item>"
        for slug, ago in entries
    )
    return f'<rss version="2.0"><channel>{items}</channel></rss>'.encode()


def home_page(*feeds: str) -> bytes:
    head = "".join(
        f'<link rel="alternate" type="application/rss+xml" href="{href}">' for href in feeds
    )
    return f"<html><head>{head}</head><body></body></html>".encode()


def robots(*sitemaps: str, disallow_all: bool = False) -> bytes:
    lines = ["User-agent: *", "Disallow: /" if disallow_all else "Allow: /"]
    lines += [f"Sitemap: {url}" for url in sitemaps]
    return "\n".join(lines).encode()


def status(url: str, code: int) -> FetchResult:
    return FetchResult(url=url, status=code, content=b"", error=None)
