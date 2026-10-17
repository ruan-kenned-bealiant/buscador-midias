"""Evidência conservadora do tipo de página, sem depender só do formato da URL."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urlsplit

from lxml import etree
from lxml import html as lxml_html

from buscador_midias.domain.url import canonicalize_url
from buscador_midias.extraction.html import decode_html

_SERVICES = frozenset(
    {
        "login",
        "logout",
        "signin",
        "sign-in",
        "signup",
        "sign-up",
        "register",
        "account",
        "privacy",
        "privacy-policy",
        "privacidade",
        "politica-de-privacidade",
        "terms",
        "terms-and-conditions",
        "cookies",
        "contact",
        "contact-us",
        "contato",
        "about",
        "about-us",
        "sobre",
        "subscribe",
        "subscription",
        "assine",
        "newsletter",
        "newsletters",
        "faq",
        "help",
        "ajuda",
    }
)
_SERVICE_TYPES = frozenset({"AboutPage", "ContactPage", "CheckoutPage", "ProfilePage"})
_EURONEWS_FORECASTS = frozenset({
    "weather", "idojaras", "meteo", "wetter", "hava-durumu", "tempo",
    "meteorologia", "tiempo",
})
_MAX_SCRIPT_CHARS = 2 * 1024 * 1024
_MAX_NODES = 10000


@dataclass(frozen=True, slots=True)
class PageClassification:
    """Tipo reconhecido e evidência; ausência de prova mantém a página desconhecida."""

    page_type: str = "unknown"
    basis: str = ""


def classify_url(url: str) -> PageClassification:
    """Reconhece serviços na raiz e diretórios observados em hosts específicos.

    Um caminho editorial como /mundo/privacy-policy continua candidato: uma palavra
    isolada no meio do caminho não basta para excluí-lo.
    """
    parts = urlsplit(url)
    segments = [s.casefold() for s in parts.path.split("/") if s]
    host = (parts.hostname or "").removeprefix("www.")
    # Diretórios de serviços observados na varredura. Limitar por host evita
    # excluir editorias de clima ou notícias sobre atendimento em outros sites.
    if (
        host in {"tvguide.vg.no", "tempo.folha.uol.com.br"}
        or (
            (host == "euronews.com" or host.endswith(".euronews.com"))
            and bool(segments) and segments[0] in _EURONEWS_FORECASTS
        )
        or (host == "elnacional.com" and segments[:2] == ["lvbp", "estadisticas"])
        or (host == "blogs.mediapart.fr" and len(segments) == 2 and segments[1] == "contacts")
        or (
            host == "nikkei.com" and segments[:3] == ["nkd", "company", "us"]
            and len(segments) > 4 and segments[4] in {"chart", "history"}
        )
        or (
            host == "evene.lefigaro.fr"
            and (segments[:1] == ["citations"] or segments[:2] == ["celebre", "biographie"])
        )
        or (
            host == "correiodoestado.com.br"
            and "atendimento" in segments
        )
    ):
        return PageClassification("service", "observed_service_route")
    if len(segments) == 1 and segments[0] in _SERVICES:
        return PageClassification("service", "service_path")
    return PageClassification()


def classify_page(
    html: bytes,
    url: str,
    *,
    parsed: etree._Element | None = None,
) -> PageClassification:
    """Combina schema, identidade da página e metadados, sem aceitar og:type isolado.

    Schemas de artigos destacados com URL diferente não classificam a seção que os
    contém. JSON inválido ou evidência conflitante mantém a página para revisão.
    """
    by_url = classify_url(url)
    if by_url.page_type != "unknown":
        return by_url
    tree = parsed
    if tree is None:
        try:
            tree = lxml_html.fromstring(
                decode_html(html).encode("utf-8"), parser=lxml_html.HTMLParser(encoding="utf-8")
            )
        except (etree.ParserError, ValueError):
            return PageClassification()
    headings = [" ".join(str(t) for t in h.itertext()) for h in tree.iter("h1")]
    og_article = any(
        m.get("property") == "og:type" and m.get("content") == "article" for m in tree.iter("meta")
    )
    current = canonicalize_url(url)
    findings: set[tuple[str, str]] = set()
    for script in tree.iter("script"):
        if (script.get("type") or "").split(";")[0].strip().casefold() != "application/ld+json":
            continue
        text = script.text or ""
        if len(text) > _MAX_SCRIPT_CHARS:
            return PageClassification("unknown", "schema_limit")
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            continue
        nodes, complete = _objects(data)
        if not complete:
            return PageClassification("unknown", "schema_limit")
        for node in nodes:
            types = _types(node.get("@type"))
            identities = _identities(node)
            matches = any(canonicalize_url(u) == current for u in identities)
            article = any(
                t.endswith("Article") or t in ("BlogPosting", "LiveBlogPosting") for t in types
            )
            dated_headline = (
                isinstance(node.get("headline"), str)
                and bool(node["headline"])
                and (isinstance(node.get("datePublished"), str) and bool(node["datePublished"]))
            )
            if matches and article and dated_headline:
                findings.add(("article", "self_article_schema"))
            elif (
                not identities
                and article
                and dated_headline
                and og_article
                and len(headings) == 1
                and _text(node["headline"]) == _text(headings[0])
            ):
                findings.add(("article", "article_heading_and_date"))
            if matches and "CollectionPage" in types:
                findings.add(("section", "self_collection_schema"))
            if matches and types.intersection(_SERVICE_TYPES):
                findings.add(("service", "self_service_schema"))
    if len({kind for kind, _ in findings}) > 1:
        return PageClassification("unknown", "conflicting_schema")
    if findings:
        kind, basis = sorted(findings)[0]
        return PageClassification(kind, basis)
    return PageClassification()


def _types(value: Any) -> set[str]:
    """Tipos schema.org, tanto em lista quanto com prefixo de URL."""
    values = value if isinstance(value, list) else [value]
    return {v.rsplit("/", 1)[-1].rsplit("#", 1)[-1] for v in values if isinstance(v, str)}


def _identities(node: dict[str, Any]) -> list[str]:
    """Prioriza url e mainEntityOfPage sobre o identificador local do objeto."""
    for key in ("url", "mainEntityOfPage", "@id"):
        value = node.get(key)
        if isinstance(value, dict):
            value = value.get("@id") or value.get("url")
        values = value if isinstance(value, list) else [value]
        urls = [u for u in values if isinstance(u, str) and u.startswith(("http://", "https://"))]
        if urls:
            return urls
    return []


def _objects(value: Any) -> tuple[list[dict[str, Any]], bool]:
    """Percorre grafos JSON sem recursão e com teto de nós."""
    stack = [value]
    result: list[dict[str, Any]] = []
    visited = 0
    while stack and visited < _MAX_NODES:
        item = stack.pop()
        visited += 1
        if isinstance(item, dict):
            result.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return result, not stack


def _text(value: str) -> str:
    """Normaliza título preservando o conteúdo, sem comparações aproximadas."""
    return " ".join(unicodedata.normalize("NFKC", unescape(value)).casefold().split())
