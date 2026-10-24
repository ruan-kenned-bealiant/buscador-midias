"""Descoberta por feed (RSS/Atom) e pela API REST do WordPress, como alternativa ao sitemap.

Só a parte pura: achar o que a home declara e ler o feed. O download fica com quem chama (que
precisa respeitar o ``robots.txt`` e o intervalo por host). O XML é conteúdo de terceiros: sem
entidades externas e sem acesso à rede, como no leitor de sitemap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from lxml import etree
from lxml import html as lxml_html

from buscador_midias.domain.feed import FeedEntry

_FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml", "application/rdf+xml"})
_ITEM_NAMES = frozenset({"item", "entry"})
_DATE_NAMES = frozenset({"pubdate", "updated", "published", "date"})
_ROOT_KINDS = {"rss": "rss", "feed": "atom", "rdf": "rdf"}
_WORDPRESS_REL = "https://api.w.org/"


@dataclass(frozen=True, slots=True)
class FeedSummary:
    """O que um feed publica no momento.

    Attributes:
        kind: ``rss``, ``atom`` ou ``rdf``.
        items: Quantos itens (``item`` ou ``entry``) o feed traz.
        newest: Data mais recente entre os itens (UTC), ou ``None`` se nenhum tem data legível.
    """

    kind: str
    items: int
    newest: datetime | None


def find_feed_links(html: bytes | str, base_url: str) -> tuple[str, ...]:
    """Feeds que a página declara em ``<link rel="alternate">``, com URL absoluta e sem repetir.

    Feeds de comentários ficam de fora (não são notícias).

    Args:
        html: Conteúdo da home (HTML tolerante a erro).
        base_url: URL da página, para resolver endereços relativos.

    Returns:
        Os endereços na ordem em que aparecem; vazio se a página não declara nenhum.
    """
    found: list[str] = []
    for link in _links(html):
        rel = (link.get("rel") or "").lower().split()
        kind = (link.get("type") or "").lower().strip()
        href = (link.get("href") or "").strip()
        title = (link.get("title") or "").lower()
        if "alternate" not in rel or kind not in _FEED_TYPES or not href:
            continue
        if "comment" in href.lower() or "comment" in title:
            continue
        try:
            url = urljoin(base_url, href)
        except ValueError:
            continue
        if url not in found:
            found.append(url)
    return tuple(found)


def find_wordpress_api(html: bytes | str, base_url: str) -> str | None:
    """Endereço da API REST do WordPress (``…/wp-json/``) declarado pela página, se houver."""
    for link in _links(html):
        rel = (link.get("rel") or "").strip()
        href = (link.get("href") or "").strip()
        if rel == _WORDPRESS_REL and href:
            return urljoin(base_url, href)
    return None


def parse_feed(xml: bytes) -> FeedSummary | None:
    """Lê um feed RSS 2.0, Atom ou RSS 1.0 (RDF).

    Args:
        xml: Corpo da resposta.

    Returns:
        O resumo, ou ``None`` se o conteúdo não for um feed (HTML, sitemap, texto, XML quebrado).
    """
    root = _parse_xml(xml)
    if root is None:
        return None
    kind = _ROOT_KINDS.get(_local(root))
    if kind is None:
        return None
    items = [el for el in root.iter() if isinstance(el.tag, str) and _local(el) in _ITEM_NAMES]
    dates = [d for item in items if (d := _item_date(item)) is not None]
    return FeedSummary(kind, len(items), max(dates) if dates else None)


def parse_feed_entries(
    xml: bytes, base_url: str, *, publication_only: bool = False
) -> tuple[FeedEntry, ...]:
    """Itens de um feed RSS 2.0, Atom ou RSS 1.0, na ordem em que o feed os lista.

    No Atom vale o ``<link rel="alternate">`` (ou o primeiro ``<link>`` sem ``rel``); no RSS, o
    texto de ``<link>``. Endereços relativos são resolvidos contra ``base_url``; itens sem link ou
    com esquema que não seja http(s) ficam de fora.

    Args:
        xml: Corpo da resposta.
        base_url: Endereço do feed, para resolver links relativos.
        publication_only: Usa somente published/pubDate, sem confundir updated com publicação.

    Returns:
        Os itens; vazio se o conteúdo não for um feed.
    """
    root = _parse_xml(xml)
    if root is None or _ROOT_KINDS.get(_local(root)) is None:
        return ()
    entries = []
    for item in root.iter():
        if not isinstance(item.tag, str) or _local(item) not in _ITEM_NAMES:
            continue
        link = _item_link(item)
        if not link:
            continue
        url = urljoin(base_url, link)
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        entries.append(
            FeedEntry(
                url, _child_text(item, "title"), _item_date(item, publication_only=publication_only)
            )
        )
    return tuple(entries)


def _item_link(item: etree._Element) -> str:
    """Link da matéria: ``href`` do ``alternate`` (Atom) ou texto de ``<link>`` (RSS e RDF)."""
    fallback = ""
    for child in item:
        if not isinstance(child.tag, str) or _local(child) != "link":
            continue
        href = (child.get("href") or "").strip()
        if href:
            rel = (child.get("rel") or "alternate").lower()
            if rel == "alternate":
                return href
            continue
        fallback = fallback or (child.text or "").strip()
    return fallback


def _child_text(item: etree._Element, name: str) -> str:
    """Texto do primeiro filho com o nome dado, sem espaços sobrando."""
    for child in item:
        if isinstance(child.tag, str) and _local(child) == name:
            return " ".join((child.text or "").split())
    return ""


def _links(html: bytes | str) -> list[etree._Element]:
    """Elementos ``<link>`` do documento; HTML ruim ou vazio dá lista vazia."""
    if not html or not html.strip():
        return []
    try:
        root = lxml_html.fromstring(html)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return []
    return list(root.iter("link"))


def _parse_xml(xml: bytes) -> etree._Element | None:
    """XML sem entidades externas nem rede; ``None`` se não der para ler."""
    if not xml or not xml.strip():
        return None
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        return etree.fromstring(xml, parser)
    except etree.XMLSyntaxError:
        return None


def _local(element: etree._Element) -> str:
    """Nome da tag sem o espaço de nomes, em minúsculas."""
    return etree.QName(element).localname.lower()


def _item_date(item: etree._Element, *, publication_only: bool = False) -> datetime | None:
    """Data de publicação de um item (a mais nova entre as que ele declara), em UTC."""
    dates = []
    names = {"pubdate", "published"} if publication_only else _DATE_NAMES
    for child in item:
        if isinstance(child.tag, str) and _local(child) in names:
            parsed = _parse_date((child.text or "").strip())
            if parsed is not None:
                dates.append(parsed)
    return max(dates) if dates else None


def _parse_date(raw: str) -> datetime | None:
    """Data RFC 822 (RSS) ou ISO 8601 (Atom e RDF); sem fuso vale UTC. ``None`` se ilegível."""
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    try:
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
