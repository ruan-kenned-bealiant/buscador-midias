"""Comparação entre duas cópias da mesma página (ex.: download direto x Bright Data)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

from buscador_midias.extraction.html import extract_article

_COMPARED_CHARS = 2000


@dataclass(frozen=True, slots=True)
class PageComparison:
    """Quão parecidas são duas cópias da mesma matéria.

    Attributes:
        text_similarity: Semelhança (0 a 1) do início do texto extraído.
        same_title: Os títulos extraídos são iguais.
        same_published_at: As datas de publicação extraídas são iguais.
        same_language: Os idiomas extraídos são iguais.
        length_ratio: Menor texto sobre o maior (1 = mesmo tamanho; baixo = uma cópia está cortada).
    """

    text_similarity: float
    same_title: bool
    same_published_at: bool
    same_language: bool
    length_ratio: float


def compare_pages(first: bytes, second: bytes, *, now: datetime) -> PageComparison:
    """Compara o conteúdo extraído de duas cópias de uma página.

    Args:
        first: HTML da primeira cópia.
        second: HTML da segunda cópia.
        now: Instante de referência da extração.

    Returns:
        A comparação; nunca levanta (cópia vazia dá similaridade 0).
    """
    a = extract_article(first, now=now, max_chars=_COMPARED_CHARS)
    b = extract_article(second, now=now, max_chars=_COMPARED_CHARS)
    longest = max(a.text_chars, b.text_chars)
    if not a.text and not b.text:
        similarity = 1.0 if not first and not second else 0.0
    else:
        similarity = SequenceMatcher(None, a.text, b.text, autojunk=False).ratio()
    return PageComparison(
        text_similarity=similarity,
        same_title=a.title == b.title,
        same_published_at=a.published_at == b.published_at,
        same_language=a.language == b.language,
        length_ratio=(min(a.text_chars, b.text_chars) / longest) if longest else 0.0,
    )
