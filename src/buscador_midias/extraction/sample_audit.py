"""Verificação de amostras de notícias sem IA ou seleção automática de mecanismos."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from lxml import html as lxml_html

from buscador_midias.extraction.html import decode_html, extract_article
from buscador_midias.extraction.reliability import PageSnapshot, assess_evidence
from buscador_midias.strategy.page_classification import classify_page


def schema_types(body: str) -> set[str]:
    """Lê tipos estruturados sem confundir um player embutido com podcast dedicado."""
    types: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            kind = value.get('@type', [])
            for name in kind if isinstance(kind, list) else [kind]:
                if isinstance(name, str):
                    types.add(name.rsplit('/', 1)[-1].lower())
            for item in value.values():
                if isinstance(item, (dict, list)):
                    visit(item)

    try:
        tree: Any = lxml_html.document_fromstring(body)
    except (ValueError, lxml_html.etree.ParserError):
        return types
    for script in tree.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            visit(json.loads(script))
        except (ValueError, TypeError):
            continue
    return types


def audit_page(
    page: PageSnapshot, *, since: datetime, until: datetime, now: datetime,
) -> dict[str, Any]:
    """Avalia conteúdo, identidade de artigo e publicação na janela de forma independente."""
    if (page.content_type or '').lower().startswith('audio/'):
        return dict(
            categoria='audio_podcast', artigo_acessivel=False, texto_util_confirmado=False,
            caracteres_texto=0, titulo='', trecho='', publicado_em='',
            publicacao_na_janela='sem_data', tipos_schema='', sinal_audio=True,
            audio_dedicado=True, classificacao_pagina='audio', motivos='mime_audio',
        )
    content = extract_article(page.html, content_type=page.content_type, max_chars=1000, now=now)
    classification = classify_page(page.html, page.final_url or page.url)
    assessment = assess_evidence(page, expected_headline='', now=now)
    reasons = set(assessment.reasons)
    if classification.page_type == 'article':
        reasons.discard('not_article')
    if classification.page_type in ('section', 'service', 'taxonomy'):
        reasons.add('not_article')
    types = schema_types(decode_html(page.html, page.content_type))
    audio = bool(types & {'podcastepisode', 'podcastseries', 'audioobject'})
    article_schema = any(name.endswith('article') for name in types)
    dedicated_audio = audio and not article_schema
    accessible = bool(content.title) and not (reasons - {'thin_content', 'paywall'})
    readable = accessible and not reasons
    if dedicated_audio:
        category = 'audio_podcast'
    elif readable:
        category = 'noticia_textual'
    elif 'challenge_page' in reasons or page.status in (401, 403, 429):
        category = 'bloqueio'
    elif 'not_article' in reasons and not (reasons & {'http_status', 'error_page'}):
        category = 'pagina_sem_artigo_confirmado'
    elif accessible:
        category = 'artigo_sem_texto_suficiente'
    else:
        category = 'inconclusivo'
    published = content.published_at
    return dict(
        categoria=category, artigo_acessivel=accessible,
        texto_util_confirmado=readable and not dedicated_audio,
        caracteres_texto=content.text_chars, titulo=content.title or '',
        trecho=content.text, publicado_em=published.isoformat() if published else '',
        publicacao_na_janela=(
            'sim' if since <= published <= until else 'nao') if published else 'sem_data',
        tipos_schema=' | '.join(sorted(types)), sinal_audio=audio,
        audio_dedicado=dedicated_audio, classificacao_pagina=classification.page_type,
        motivos=' | '.join(sorted(reasons)),
    )
