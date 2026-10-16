"""Filtro pela forma da URL (L0): decide sem baixar nada.

O objetivo é descartar o que **certamente** não é uma matéria noticiosa (página inicial, listagens
de tag/autor/categoria, seções, arquivos de mídia) e, **se pedido**, seções de temas sem interesse
(esporte, culinária, horóscopo). Na dúvida a URL passa: descartar uma matéria relevante
custa mais do que analisar uma página a mais.
"""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import unquote, urlsplit


class FilterReason(StrEnum):
    """Por que uma URL foi descartada pela forma."""

    HOME = "home"
    LISTING = "listing"
    SECTION = "section"
    ASSET = "asset"
    OFF_TOPIC = "off_topic"


DEFAULT_OFF_TOPIC_SEGMENTS: frozenset[str] = frozenset(
    {
        "esporte",
        "esportes",
        "futebol",
        "sports",
        "deportes",
        "receitas",
        "culinaria",
        "recipes",
        "horoscopo",
        "horoscope",
    }
)

# Segmentos de caminho que só existem em páginas de listagem, em vários idiomas.
_LISTING_SEGMENTS: frozenset[str] = frozenset(
    {
        "tag",
        "tags",
        "keyword",
        "keywords",
        "topic",
        "topics",
        "author",
        "authors",
        "autor",
        "autores",
        "auteur",
        "category",
        "categories",
        "categoria",
        "categorias",
        "etiqueta",
        "etiquetas",
        "page",
        "search",
        "busca",
    }
)

_ASSET_EXTENSIONS: frozenset[str] = frozenset(
    {
        "jpg",
        "jpeg",
        "png",
        "gif",
        "webp",
        "svg",
        "ico",
        "mp3",
        "mp4",
        "webm",
        "zip",
        "xml",
        "json",
        "css",
        "js",
    }
)

_EXTENSION = re.compile(r"\.([a-z0-9]{2,5})$")


def l0_filter(url: str, *, off_topic_segments: frozenset[str] = frozenset()) -> FilterReason | None:
    """Indica se a URL deve ser descartada pela sua forma.

    Regras, em ordem de precedência:

    - ``HOME``: caminho vazio.
    - ``ASSET``: termina em extensão de mídia ou dado (imagem, vídeo, XML...). PDF não entra.
    - ``LISTING``: algum segmento é de listagem (``tag``, ``author``, ``category``, ``page``...).
    - ``SECTION``: um único segmento, só letras (sem hífen, dígito ou ponto): é seção ou página
      institucional, pois o *slug* de uma matéria quase sempre tem várias palavras.
    - ``OFF_TOPIC``: algum segmento **antes do último** está em ``off_topic_segments`` (o último é
      o *slug* da matéria e não conta).

    Args:
        url: URL a avaliar.
        off_topic_segments: Segmentos de seções sem interesse (ex.: ``DEFAULT_OFF_TOPIC_SEGMENTS``).
            Por padrão vazio: descartar por tema é decisão explícita, pois uma seção de esporte
            também traz matérias de gestão e denúncia.

    Returns:
        O motivo do descarte, ou ``None`` se a URL deve seguir adiante. Nunca levanta: uma URL
        malformada passa.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    segments = [unquote(part).lower() for part in path.split("/") if part]
    if not segments:
        return FilterReason.HOME if url else None

    extension = _EXTENSION.search(segments[-1])
    if extension and extension.group(1) in _ASSET_EXTENSIONS:
        return FilterReason.ASSET
    if any(part in _LISTING_SEGMENTS for part in segments):
        return FilterReason.LISTING
    if len(segments) == 1 and re.fullmatch(r"[^\W\d_]+", segments[0]):
        return FilterReason.SECTION
    if any(part in off_topic_segments for part in segments[:-1]):
        return FilterReason.OFF_TOPIC
    return None
