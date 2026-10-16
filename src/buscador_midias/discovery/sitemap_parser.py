"""Leitura defensiva de sitemaps (XML, ``.xml.gz`` e ``.zip``) vindos de sites de terceiros."""

from __future__ import annotations

import io
import zipfile
import zlib
from datetime import UTC, datetime

from lxml import etree

from buscador_midias.domain.sitemap import (
    ParsedSitemap,
    SitemapEntry,
    SitemapKind,
    SitemapRef,
)

# O protocolo de sitemaps limita cada arquivo a 50 MB descomprimido.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06")  # com arquivos; zip vazio
# BOM UTF-8 e espaços antes da declaração <?xml ?> tornam o XML inválido; vários sites os emitem.
_LEADING_JUNK = b"\xef\xbb\xbf \t\r\n"
# Páginas de bloqueio, de erro e 404 "suave" costumam voltar como HTML com status 200.
_HTML_PREFIXES = (b"<!doctype html", b"<html")


class SitemapParseError(ValueError):
    """O conteúdo não é um sitemap válido ou excede os limites de segurança."""


def parse_sitemap(content: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES) -> ParsedSitemap:
    """Interpreta um sitemap, descomprimindo gzip ou zip quando necessário.

    O conteúdo é não confiável: entidades externas e acesso à rede ficam desligados e o tamanho
    descomprimido é limitado, para se proteger de XXE e de gzip bombs.

    Args:
        content: Bytes do arquivo, em XML puro, gzip ou zip (de um único XML).
        max_bytes: Tamanho máximo do XML descomprimido.

    Returns:
        O sitemap interpretado (índice ou lista de URLs). Entradas sem ``loc`` são ignoradas e
        datas inválidas viram ``None``.

    Raises:
        SitemapParseError: XML inválido, gzip/zip inválido ou truncado, zip com mais de um arquivo,
            raiz que não é sitemap ou conteúdo acima do limite.
    """
    if content.startswith(_GZIP_MAGIC):
        xml = _decompress(content, max_bytes)
    elif content.startswith(_ZIP_MAGICS):
        xml = _unzip(content, max_bytes)
    else:
        xml = content
    if len(xml) > max_bytes:
        raise SitemapParseError(f"conteúdo acima do limite de {max_bytes} bytes")

    xml = xml.lstrip(_LEADING_JUNK)
    if xml[:15].lower().startswith(_HTML_PREFIXES):
        raise SitemapParseError("a resposta é uma página HTML, não um sitemap")

    root = _parse_xml(xml)
    root_name = _local_name(root)
    if root_name == "sitemapindex":
        return _parse_index(root)
    if root_name == "urlset":
        return _parse_urlset(root)
    raise SitemapParseError(f"raiz inesperada: <{root_name}>")


def _decompress(content: bytes, max_bytes: int) -> bytes:
    """Descomprime gzip limitando a saída a ``max_bytes``."""
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    try:
        data = decompressor.decompress(content, max_bytes + 1)
    except zlib.error as error:
        raise SitemapParseError(f"gzip inválido: {error}") from error
    if len(data) > max_bytes or decompressor.unconsumed_tail:
        raise SitemapParseError(f"conteúdo descomprimido acima do limite de {max_bytes} bytes")
    if not decompressor.eof:
        raise SitemapParseError("gzip truncado")
    return data


def _unzip(content: bytes, max_bytes: int) -> bytes:
    """Lê o único arquivo de um zip limitando a saída a ``max_bytes``.

    O tamanho declarado no zip não é confiável (bomba de descompressão): o limite vale para o que
    se lê de fato. Um zip com vários arquivos é recusado, em vez de ler só um e perder o resto.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if not names:
                raise SitemapParseError("zip vazio")
            if len(names) > 1:
                raise SitemapParseError(f"zip com vários arquivos ({len(names)}), não suportado")
            with archive.open(names[0]) as member:
                data = member.read(max_bytes + 1)
    except zipfile.BadZipFile as error:
        raise SitemapParseError(f"zip inválido: {error}") from error
    except (zlib.error, EOFError, NotImplementedError) as error:
        raise SitemapParseError(f"zip inválido: {error}") from error
    if len(data) > max_bytes:
        raise SitemapParseError(f"conteúdo descomprimido acima do limite de {max_bytes} bytes")
    return data


def _parse_xml(xml: bytes) -> etree._Element:
    """Faz o parse com resolução de entidades e rede desligadas."""
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )
    try:
        return etree.fromstring(xml, parser)
    except etree.XMLSyntaxError as error:
        raise SitemapParseError(f"XML inválido: {error}") from error


def _local_name(element: etree._Element) -> str:
    """Nome da tag sem namespace."""
    return etree.QName(element).localname


def _children_text(element: etree._Element) -> dict[str, str]:
    """Texto dos filhos diretos, indexado por nome local (a primeira ocorrência vence)."""
    texts: dict[str, str] = {}
    for child in element:
        if isinstance(child.tag, str) and child.text:
            texts.setdefault(_local_name(child), child.text.strip())
    return texts


def _parse_index(root: etree._Element) -> ParsedSitemap:
    """Lê um ``sitemapindex``."""
    children: list[SitemapRef] = []
    for node in root:
        if not isinstance(node.tag, str) or _local_name(node) != "sitemap":
            continue
        fields = _children_text(node)
        if loc := fields.get("loc"):
            children.append(SitemapRef(loc=loc, lastmod=_parse_datetime(fields.get("lastmod"))))
    return ParsedSitemap(kind=SitemapKind.INDEX, children=tuple(children))


def _parse_urlset(root: etree._Element) -> ParsedSitemap:
    """Lê um ``urlset``, incluindo metadados de news sitemap."""
    entries: list[SitemapEntry] = []
    has_news = False
    for node in root:
        if not isinstance(node.tag, str) or _local_name(node) != "url":
            continue
        fields = _children_text(node)
        loc = fields.get("loc")
        if not loc:
            continue
        news = next((c for c in node if isinstance(c.tag, str) and _local_name(c) == "news"), None)
        news_fields = _children_text(news) if news is not None else {}
        has_news = has_news or news is not None
        entries.append(
            SitemapEntry(
                loc=loc,
                lastmod=_parse_datetime(fields.get("lastmod")),
                title=news_fields.get("title"),
                publication_date=_parse_datetime(news_fields.get("publication_date")),
            )
        )
    return ParsedSitemap(kind=SitemapKind.URLSET, entries=tuple(entries), has_news=has_news)


def _parse_datetime(value: str | None) -> datetime | None:
    """Converte uma data W3C/ISO 8601 para UTC; sem fuso, assume UTC; inválida vira ``None``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
