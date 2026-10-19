"""Extração do conteúdo de uma página de notícia: título, texto, data de publicação e idioma.

A data e o idioma seguem a mesma ordem de confiança do KyX (``MidiasIaPublishedAtParser`` e
``MidiasIaLanguageParser``) e saem sempre do HTML, nunca da análise de um modelo, que só vê trechos
e confundiria a data da publicação com uma data citada no corpo.
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from lxml import etree
from lxml import html as lxml_html

DEFAULT_MAX_CHARS = 3000
_MIN_PARAGRAPH_CHARS = 25
_MIN_FALLBACK_PARAGRAPH_CHARS = 40
_MIN_YEAR = 1990
_MAX_LANGUAGE_CHARS = 10
_NOISE_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "aside", "form", "iframe")
_ARTICLE_TYPE = re.compile(
    r'"@type"\s*:\s*"?([^"}]*Article|NewsArticle|ReportageNewsArticle)"?', re.I
)
_DATE_META = (
    '//meta[@property="article:published_time"]/@content',
    '//meta[@name="article:published_time"]/@content',
    '//meta[@itemprop="datePublished"]/@content',
    '//meta[@name="publish-date"]/@content',
    '//meta[@property="og:article:published_time"]/@content',
)
_PUBLISH_WORDS = ("publish", "pubdate", "date.issued")
_WHITESPACE = re.compile(r"\s+")
_CJK = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_POOR_PARAGRAPH_CHARS = 150
_USABLE_BLOCK_CHARS = 300


@dataclass(frozen=True, slots=True)
class ArticleContent:
    """O que se extrai de uma página de notícia.

    Attributes:
        title: Melhor título (``og:title``, depois ``<title>``, depois o primeiro ``<h1>``).
        title_candidates: Todos os títulos encontrados (para conferir se a página é a esperada).
        description: ``og:description`` ou ``meta description``.
        text: Início do texto do artigo, limitado a ``max_chars``.
        text_chars: Tamanho do texto completo extraído (antes do limite): mede a substância.
        published_at: Data de publicação (UTC), só se plausível.
        language: Idioma do ``<html lang>`` ou do ``og:locale``.
        canonical_url: URL canônica declarada pela página.
        has_article_data: Há dados estruturados de artigo ou a tag ``<article>``.
        is_article: A página se declara uma matéria: JSON-LD de artigo, ``og:type=article`` ou data
            de publicação em ``meta``/JSON-LD. A tag ``<article>`` e o ``<time>`` não bastam, pois
            páginas de seção os usam em cada destaque.
    """

    title: str | None
    title_candidates: tuple[str, ...]
    description: str | None
    text: str
    text_chars: int
    published_at: datetime | None
    language: str | None
    canonical_url: str | None
    has_article_data: bool
    is_article: bool


_EMPTY = ArticleContent(None, (), None, "", 0, None, None, None, False, False)
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_META_CHARSET = re.compile(rb"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_SNIFF_BYTES = 4096


def decode_html(content: bytes, content_type: str | None = None) -> str:
    """Texto de um HTML em bytes, sem estragar os acentos (``Ã©`` no lugar de ``é``).

    Ordem: BOM; UTF-8 válido (é prova forte e vence uma declaração errada); charset do cabeçalho
    ``Content-Type``; charset do ``<meta>``; por último Windows-1252, que aceita qualquer byte.
    Nunca levanta.

    Args:
        content: Corpo da resposta.
        content_type: Cabeçalho ``Content-Type``, se houve.

    Returns:
        O texto decodificado.
    """
    if not content:
        return ""
    for bom, codec in _BOMS:
        if content.startswith(bom):
            return content.decode(codec, errors="replace")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    declared = []
    if content_type and (match := _HEADER_CHARSET.search(content_type)):
        declared.append(match.group(1))
    if match_meta := _META_CHARSET.search(content[:_SNIFF_BYTES]):
        declared.append(match_meta.group(1).decode("ascii", errors="ignore"))
    for charset in declared:
        try:
            return content.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("cp1252", errors="replace")


def extract_article(
    source: str | bytes,
    *,
    now: datetime | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    content_type: str | None = None,
) -> ArticleContent:
    """Extrai título, texto, data e idioma de uma página.

    Nunca levanta: HTML vazio, quebrado ou ilegível devolve um conteúdo vazio.

    Args:
        source: HTML (texto ou bytes; bytes passam por ``decode_html``).
        now: Instante de referência para rejeitar datas futuras (padrão: agora, em UTC).
        max_chars: Limite do ``text`` devolvido.
        content_type: Cabeçalho ``Content-Type`` da resposta (ajuda a decodificar os bytes).

    Returns:
        O conteúdo extraído.
    """
    root = _parse(decode_html(source, content_type) if isinstance(source, bytes) else source)
    if root is None:
        return _EMPTY
    now = now or datetime.now(UTC)

    json_ld = _json_ld_nodes(root)
    title_candidates = _title_candidates(root)
    body_text = _article_text(root, json_ld)
    return ArticleContent(
        title=title_candidates[0] if title_candidates else None,
        title_candidates=title_candidates,
        description=_first_attr(root, '//meta[@property="og:description"]/@content')
        or _first_attr(root, '//meta[@name="description"]/@content'),
        text=body_text[:max_chars],
        text_chars=len(body_text),
        published_at=_published_at(root, json_ld, now),
        language=_language(root),
        canonical_url=_first_attr(root, '//link[@rel="canonical"]/@href')
        or _first_attr(root, '//meta[@property="og:url"]/@content'),
        has_article_data=_has_article_data(root, json_ld),
        is_article=_is_article(root, json_ld, now),
    )


def _parse(source: str | bytes) -> Any | None:
    """Árvore HTML, ou ``None`` se não for possível interpretá-la."""
    if not source:
        return None
    if isinstance(source, str):
        source = re.sub(r"^\s*<\?xml[^>]*\?>", "", source)
    try:
        return lxml_html.document_fromstring(source)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return None


def _clean(value: str | None) -> str | None:
    """Texto com espaços normalizados, ou ``None`` se ficar vazio."""
    text = _WHITESPACE.sub(" ", value or "").strip()
    return text or None


def _first_attr(root: Any, xpath: str) -> str | None:
    """Primeiro valor não vazio do XPath."""
    for value in root.xpath(xpath):
        cleaned = _clean(str(value))
        if cleaned:
            return cleaned
    return None


def _title_candidates(root: Any) -> tuple[str, ...]:
    """Títulos em ordem de preferência: ``og:title``, ``<title>`` e primeiro ``<h1>``."""
    found = [
        _first_attr(root, '//meta[@property="og:title"]/@content'),
        _clean(" ".join(root.xpath("//title//text()"))),
        _clean(" ".join(root.xpath("(//h1)[1]//text()"))),
    ]
    unique: list[str] = []
    for title in found:
        if title and title not in unique:
            unique.append(title)
    return tuple(unique)


def _json_ld_nodes(root: Any) -> list[dict[str, Any]]:
    """Objetos JSON-LD da página (achatando listas e ``@graph``)."""
    nodes: list[dict[str, Any]] = []
    for script in root.xpath('//script[@type="application/ld+json"]'):
        try:
            decoded = json.loads(script.text_content())
        except ValueError:
            continue
        entries = (
            decoded
            if isinstance(decoded, list)
            else decoded.get("@graph", [decoded])
            if isinstance(decoded, dict)
            else []
        )
        nodes.extend(entry for entry in entries if isinstance(entry, dict))
    return nodes


def _has_article_data(root: Any, json_ld: list[dict[str, Any]]) -> bool:
    """Se a página declara um artigo (JSON-LD) ou usa a tag ``<article>``."""
    declared = any(_ARTICLE_TYPE.search(json.dumps(node)) for node in json_ld)
    return declared or bool(root.xpath("//article"))


def _is_article(root: Any, json_ld: list[dict[str, Any]], now: datetime) -> bool:
    """Se a página se declara uma matéria (e não uma seção, listagem ou home)."""
    declares_type = any(_ARTICLE_TYPE.search(json.dumps(node)) for node in json_ld)
    og_article = (
        _first_attr(root, '//meta[@property="og:type"]/@content') or ""
    ).lower() == "article"
    return (
        declares_type
        or og_article
        or _published_at(root, json_ld, now, use_time_tag=False) is not None
    )


def cjk_ratio(text: str) -> float:
    """Fração dos caracteres (sem espaços) que são chinês, japonês ou coreano.

    Nesses idiomas cada caractere carrega muito mais informação, então limites de tamanho de texto
    pensados para o alfabeto latino não valem.
    """
    visible = [char for char in text if not char.isspace()]
    if not visible:
        return 0.0
    return sum(1 for char in visible if _CJK.match(char)) / len(visible)


def _article_text(root: Any, json_ld: list[dict[str, Any]]) -> str:
    """Texto do artigo: parágrafos do contêiner, ``articleBody`` ou, sem eles, o texto dos blocos.

    Sites que não usam ``<p>`` (texto em ``<div>`` com ``<br>``) caem para o texto do
    ``<article>``/``<main>`` e, se este for minúsculo, para o do corpo sem menu e rodapé.
    """
    etree.strip_elements(root, *_NOISE_TAGS, with_tail=False)
    articles = root.xpath("//article")
    container = max(articles, key=lambda el: len(el.text_content())) if articles else None
    if container is None:
        mains = root.xpath("//main")
        container = mains[0] if mains else None
    minimum = _MIN_PARAGRAPH_CHARS if container is not None else _MIN_FALLBACK_PARAGRAPH_CHARS
    scope = container if container is not None else root
    paragraphs = [
        text
        for text in (_clean(p.text_content()) for p in scope.iter("p"))
        if text and len(text) >= minimum
    ]
    from_paragraphs = "\n".join(paragraphs)
    bodies = [
        str(node["articleBody"]) for node in json_ld if isinstance(node.get("articleBody"), str)
    ]
    from_json = _clean(max(bodies, key=len)) if bodies else None
    best = max([from_paragraphs, from_json or ""], key=len)
    if len(best) >= _POOR_PARAGRAPH_CHARS:
        return best
    return max([best, _block_text(root, container)], key=len)


def _block_text(root: Any, container: Any | None) -> str:
    """Texto corrido do contêiner do artigo ou, se for pequeno demais, do corpo da página."""
    scopes = [container] if container is not None else []
    bodies = root.xpath("//body")
    scopes.append(bodies[0] if bodies else root)
    text = ""
    for scope in scopes:
        text = _clean(scope.text_content()) or ""
        if len(text) >= _USABLE_BLOCK_CHARS:
            break
    return text


def _published_at(
    root: Any, json_ld: list[dict[str, Any]], now: datetime, *, use_time_tag: bool = True
) -> datetime | None:
    """Data de publicação: meta ``article:published_time``, depois JSON-LD, depois ``<time>``."""
    candidates: list[str | None] = [_first_attr(root, query) for query in _DATE_META]
    candidates += [node.get("datePublished") for node in json_ld]
    candidates += _publish_metas(root)
    if use_time_tag:
        candidates += [_first_attr(root, "//time[@datetime]/@datetime")]
    for value in candidates:
        parsed = _parse_date(value, now) if isinstance(value, str) else None
        if parsed is not None:
            return parsed
    return None


def _publish_metas(root: Any) -> list[str]:
    """Valores de ``meta`` cujo nome indica data de publicação (variações de CMS e agências)."""
    values = []
    for meta in root.xpath("//meta[@content]"):
        name = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
        if any(word in name for word in _PUBLISH_WORDS):
            values.append(meta.get("content"))
    return values


def _parse_date(value: str, now: datetime) -> datetime | None:
    """Interpreta a data (ISO 8601 ou RFC 2822), sem fuso = UTC; descarta lixo e datas futuras."""
    text = value.strip()
    if not text:
        return None
    parsed: datetime | None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        parsed = parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
    if parsed.year < _MIN_YEAR or parsed > now:
        return None
    return parsed


def _language(root: Any) -> str | None:
    """Idioma do ``<html lang>``, com ``og:locale`` de reserva; ignora valores longos demais."""
    candidates = [
        _first_attr(root, "//html/@lang"),
        (_first_attr(root, '//meta[@property="og:locale"]/@content') or "").replace("_", "-"),
    ]
    for value in candidates:
        if value and len(value) <= _MAX_LANGUAGE_CHARS:
            return value
    return None
