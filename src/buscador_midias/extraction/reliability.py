"""Avaliação de confiabilidade de uma página como evidência.

Uma evidência só vale se for **a página da matéria, íntegra e legível**: não uma página de erro com
status 200, nem um desafio de bot, nem a home para onde o site redirecionou, nem um resumo atrás de
paywall. Cada critério reprovado vira um código de motivo, para ser gravado e auditado.

A detecção de página de erro segue a do KyX (``looksLikeErrorPage``); os demais critérios existem
porque aqui o HTML pode vir de um download direto, sem o navegador renderizado do Bright Data.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from lxml import etree
from lxml import html as lxml_html

from buscador_midias.extraction.html import cjk_ratio, decode_html, extract_article

MIN_HTML_BYTES = 1500
MIN_TEXT_CHARS = 500
MIN_TEXT_CHARS_CJK = 150  # um caractere chinês, japonês ou coreano rende várias letras latinas
CJK_THRESHOLD = 0.3
PAYWALL_MAX_TEXT_CHARS = 3000
CHALLENGE_MAX_TEXT_CHARS = 1500
MIN_EXPECTED_TOKENS = 3
MIN_TITLE_OVERLAP = 0.6
_MIN_TOKEN_LENGTH = 3
MIN_ASCII_SHARE = 0.5
_WORD = re.compile(r"[^\W\d_]+")

_ERROR_SIGNALS = (
    "404",
    "page not found",
    "not found",
    "pagina nao encontrada",
    "pagina nao existe",
    "conteudo nao encontrado",
    "conteudo indisponivel",
    "post nao encontrado",
    "nothing found",
)
_CHALLENGE_SIGNALS = (
    "just a moment",
    "attention required",
    "access denied",
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies",
    "captcha",
    "are you a robot",
    "unusual traffic",
    "acesso negado",
    "verificando seu navegador",
)
_PAYWALL_SIGNALS = (
    "assine para continuar",
    "assine para ler",
    "conteudo exclusivo para assinantes",
    "exclusivo para assinantes",
    "subscribe to continue",
    "subscribe to read",
    "sign in to read",
    "this article is for subscribers",
    "abonnez-vous pour lire",
    "pour lire la suite",
    "suscribete para seguir leyendo",
)
_ARTICLE_TYPE = re.compile(
    r'"@type"\s*:\s*"?([^"}]*Article|NewsArticle|ReportageNewsArticle)"?', re.I
)
_WWW_PREFIX = re.compile(r"^www\d*\.")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    """O que foi baixado de uma URL, o suficiente para julgá-lo como evidência.

    Attributes:
        url: URL pedida.
        status: Status HTTP final (``None`` se não houve resposta).
        content_type: Cabeçalho ``Content-Type`` (``None`` se ausente).
        final_url: URL final depois de redirecionamentos (``None`` se igual à pedida).
        html: Corpo recebido.
    """

    url: str
    status: int | None
    content_type: str | None
    final_url: str | None
    html: bytes


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    """Veredito sobre a confiabilidade de uma página como evidência.

    Attributes:
        ok: ``True`` se passou em todos os critérios.
        reasons: Códigos dos critérios reprovados (``http_status``, ``not_html``, ``too_small``,
            ``error_page``, ``challenge_page``, ``redirected_away``, ``title_mismatch``,
            ``not_article``, ``thin_content``, ``paywall``, ``entity_missing``), na ordem em que
            são avaliados.
    """

    ok: bool
    reasons: tuple[str, ...]


def assess_evidence(
    page: PageSnapshot,
    *,
    expected_headline: str,
    must_mention: Sequence[str] = (),
    now: datetime | None = None,
    min_text_chars: int | None = None,
) -> EvidenceAssessment:
    """Julga se a página serve de evidência da matéria esperada.

    Args:
        page: Página baixada.
        expected_headline: Título do news sitemap ou slug da URL (confere se é a matéria certa).
        must_mention: Entidades que a análise citou; ao menos uma precisa aparecer na página.
        now: Instante de referência (datas futuras são descartadas na extração).
        min_text_chars: Texto mínimo extraído para não ser "conteúdo fino" (paywall, esqueleto JS).
            Padrão: 500 caracteres, ou 150 em páginas de chinês, japonês e coreano.

    Returns:
        O veredito com os motivos de reprovação.
    """
    reasons: list[str] = []
    if page.status != 200:
        reasons.append("http_status")
    if not _is_html(page):
        reasons.append("not_html")
    if len(page.html) < MIN_HTML_BYTES:
        reasons.append("too_small")

    text = decode_html(page.html, page.content_type)
    content = extract_article(text, now=now)
    plain = _plain_text(text)
    title = " ".join(content.title_candidates)
    if looks_like_error_page(text, expected_title=expected_headline):
        reasons.append("error_page")
    if looks_like_challenge_page(_fold(title) + " " + plain, text_chars=content.text_chars):
        reasons.append("challenge_page")
    if _redirected_away(page):
        reasons.append("redirected_away")
    if not _title_matches(page, expected_headline, content.title_candidates, content.canonical_url):
        reasons.append("title_mismatch")
    if not content.is_article:
        reasons.append("not_article")
    if content.text_chars < _min_text_chars(min_text_chars, content.text):
        reasons.append("thin_content")
    if content.text_chars < PAYWALL_MAX_TEXT_CHARS and any(s in plain for s in _PAYWALL_SIGNALS):
        reasons.append("paywall")
    if must_mention and not any(_fold(name) in plain for name in must_mention if _fold(name)):
        reasons.append("entity_missing")
    return EvidenceAssessment(ok=not reasons, reasons=tuple(reasons))


def looks_like_error_page(html: str | bytes, expected_title: str = "") -> bool:
    """Detecta página de erro servida com status 200 (mesma regra do KyX).

    É erro se o título contém um sinal forte, se o corpo traz dois ou mais sinais, ou se traz um
    sinal, a manchete esperada não aparece e a página não declara um artigo.
    """
    plain = _plain_text(html)
    title = _fold(_title(html))
    if any(signal in title for signal in _ERROR_SIGNALS):
        return True
    hits = sum(1 for signal in _ERROR_SIGNALS if signal in plain)
    if hits >= 2:
        return True
    expected = _fold(expected_title)
    if expected and expected not in plain:
        return hits >= 1 and not _declares_article(html)
    return False


def looks_like_challenge_page(text: str, *, text_chars: int) -> bool:
    """Detecta desafio de bot/CAPTCHA/bloqueio no lugar da matéria.

    Só vale para páginas com pouco texto: a palavra "captcha" dentro de uma matéria longa não é
    um desafio.

    Args:
        text: Título e texto da página (a comparação ignora acento e caixa).
        text_chars: Tamanho do texto de artigo extraído da página.
    """
    folded = _fold(text)
    return text_chars < CHALLENGE_MAX_TEXT_CHARS and any(s in folded for s in _CHALLENGE_SIGNALS)


def _fold(text: str) -> str:
    """Minúsculas, sem acento e com espaços normalizados."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    return _WHITESPACE.sub(" ", stripped).strip()


def _tree(source: str | bytes) -> Any | None:
    """Árvore HTML (ou ``None`` se ilegível); bytes são decodificados antes."""
    if isinstance(source, bytes):
        source = decode_html(source)
    if not source:
        return None
    try:
        return lxml_html.document_fromstring(source)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return None


def _plain_text(source: str | bytes) -> str:
    """Texto visível da página (sem scripts e estilos), normalizado."""
    root = _tree(source)
    if root is None:
        return ""
    etree.strip_elements(root, "script", "style", "noscript", with_tail=False)
    return _fold(root.text_content())


def _title(source: str | bytes) -> str:
    """Conteúdo da tag ``<title>``."""
    root = _tree(source)
    if root is None:
        return ""
    return " ".join(root.xpath("//title//text()"))


def _declares_article(source: str | bytes) -> bool:
    """Se a página tem JSON-LD de artigo."""
    text = source.decode("utf-8", "ignore") if isinstance(source, bytes) else source
    return _ARTICLE_TYPE.search(text) is not None


def _is_html(page: PageSnapshot) -> bool:
    """Se o conteúdo é HTML: pelo cabeçalho ou, sem ele, pelo começo do corpo."""
    if page.content_type:
        return "html" in page.content_type.lower()
    head = page.html[:2048].lower()
    return b"<html" in head or b"<!doctype html" in head or b"<body" in head


def _host(url: str) -> str:
    """Host sem ``www``/``www1``."""
    return _WWW_PREFIX.sub("", (urlsplit(url).hostname or "").lower())


def _redirected_away(page: PageSnapshot) -> bool:
    """Se o site mandou para outro domínio ou para a home no lugar da matéria."""
    if not page.final_url:
        return False
    final, original = urlsplit(page.final_url), urlsplit(page.url)
    if _host(page.final_url) != _host(page.url):
        return True
    return final.path in ("", "/") and original.path not in ("", "/")


def _min_text_chars(configured: int | None, text: str) -> int:
    """Texto mínimo exigido: o configurado ou, por padrão, o do alfabeto latino ou o do CJK."""
    if configured is not None:
        return configured
    return MIN_TEXT_CHARS_CJK if cjk_ratio(text) >= CJK_THRESHOLD else MIN_TEXT_CHARS


def _words(text: str) -> list[str]:
    """Palavras (letras de qualquer alfabeto, 3+), sem acento e em minúsculas."""
    return [word for word in _WORD.findall(_fold(text)) if len(word) >= _MIN_TOKEN_LENGTH]


def _same_page(first: str, second: str) -> bool:
    """Se duas URLs apontam para a mesma página (mesmo site sem ``www`` e mesmo caminho)."""
    a, b = urlsplit(first), urlsplit(second)
    return (
        _host(first) == _host(second) and a.path.rstrip("/").lower() == b.path.rstrip("/").lower()
    )


def _title_matches(
    page: PageSnapshot, expected_headline: str, candidates: Sequence[str], canonical: str | None
) -> bool:
    """Se a página é a matéria esperada, pelo título.

    Não reprova quando não há como comparar: a URL canônica declarada pela página é a pedida (prova
    mais forte que o título), a manchete esperada tem menos de 3 palavras, ou o título da página
    está em outro alfabeto que o da manchete esperada (slug transliterado em latim). Fora isso, os
    títulos da página precisam cobrir 60% das palavras esperadas.
    """
    if canonical and any(_same_page(canonical, url) for url in (page.url, page.final_url) if url):
        return True
    expected = set(_words(expected_headline))
    if len(expected) < MIN_EXPECTED_TOKENS:
        return True
    found = {word for title in candidates for word in _words(title)}
    if expected.isdisjoint(found) and _different_script(expected, found):
        return True
    return len(expected & found) / len(expected) >= MIN_TITLE_OVERLAP


def _different_script(expected: set[str], found: set[str]) -> bool:
    """Se a manchete esperada é só ASCII (latim) e o título da página quase não é."""
    if not found or not all(word.isascii() for word in expected):
        return not found
    return sum(word.isascii() for word in found) / len(found) < MIN_ASCII_SHARE
