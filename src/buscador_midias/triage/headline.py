"""Texto de manchete e seus atributos: a base comum das camadas de triagem por título."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit

_TOKEN = re.compile(r"[a-z]{2,}")
_PUBLISHER_SEPARATOR = re.compile(r"\s[-|–—]\s")
_EXTENSION = re.compile(r"\.[a-z0-9]{2,5}$", re.IGNORECASE)
_WORD_SEPARATORS = re.compile(r"[-_]+")
_MAX_PUBLISHER_WORDS = 4
_MIN_HEADLINE_WORDS = 3


def tokens(text: str) -> list[str]:
    """Palavras da manchete: sem acento, minúsculas, só letras (mínimo 2) e sem o nome do veículo.

    Números e códigos colados em dígitos (``3327847e``) não viram palavras.

    Args:
        text: Título ou slug.

    Returns:
        As palavras, na ordem em que aparecem.
    """
    return _TOKEN.findall(_fold(_without_publisher(text)))


def features(text: str) -> list[str]:
    """Atributos de uma manchete: cada palavra e cada par de palavras vizinhas (``a_b``)."""
    words = tokens(text)
    return words + [f"{first}_{second}" for first, second in zip(words, words[1:], strict=False)]


def headline_text(url: str, title: str | None) -> str:
    """Melhor texto de manchete disponível: o título do news sitemap ou, sem ele, o slug da URL.

    O slug é o segmento do caminho com mais hífens (precisa de ao menos um), sem extensão e com os
    separadores trocados por espaço. URLs sem slug (seção, vídeo numerado) não têm manchete.

    Args:
        url: URL da matéria.
        title: Título informado pelo sitemap, se houver.

    Returns:
        O texto, ou ``""`` quando não há manchete.
    """
    if title and title.strip():
        return " ".join(title.split())
    return _slug(url)


def _slug(url: str) -> str:
    """Slug legível da URL (``""`` se nenhum segmento parece uma manchete)."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    segments = [unquote(part) for part in path.split("/") if part]
    if not segments:
        return ""
    best = max(segments, key=lambda part: part.count("-"))
    if best.count("-") == 0:
        return ""
    return _WORD_SEPARATORS.sub(" ", _EXTENSION.sub("", best)).strip()


def _without_publisher(text: str) -> str:
    """Tira o sufixo " - Veículo" (ou " | Veículo") quando é curto e sobra uma manchete."""
    matches = list(_PUBLISHER_SEPARATOR.finditer(text))
    if not matches:
        return text
    last = matches[-1]
    head, tail = text[: last.start()], text[last.end() :]
    if len(tail.split()) <= _MAX_PUBLISHER_WORDS and len(head.split()) >= _MIN_HEADLINE_WORDS:
        return head
    return text


def _fold(text: str) -> str:
    """Minúsculas e sem acentos."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
