"""Do achado da análise ao artigo do corpus de Mídias IA, no formato que o KyX já usa."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from unidecode import unidecode

from buscador_midias.analysis.classification import content_category
from buscador_midias.corpus.country import registered_country, resolve_country
from buscador_midias.domain.roles import is_adverse_role, normalize_role
from buscador_midias.domain.site import Site
from buscador_midias.domain.url import url_domain
from buscador_midias.state.store import CorpusCandidate

SOURCE = "buscador-midias"
ORIGIN = "crawler"
SOURCE_TIER = "trusted"  # todas as fontes vêm da lista de fontes confiáveis

# Mesma palavra-chave que o KyX grava para o tema: o nome do tema em minúsculas.
THEME_KEYWORDS: dict[str, str] = {
    "corrupcao": "corrupção",
    "financeiro": "financeiro",
    "trabalhista": "trabalhista",
    "ambiental": "ambiental",
    "fiscal": "fiscal",
    "criminal": "criminal",
    "reputacional": "reputacional",
    "etica_integridade": "ética e integridade",
    "pldft": "pld/ft",
    "processos_judiciais": "processos judiciais",
    "ciber_seguranca": "ciber segurança",
}
# Matéria com entidade mas sem tema adverso: o KyX a encontra pela categoria "Diversos" (a busca
# dele casa o nome do tema em minúsculas com a palavra-chave do artigo).
DIVERSOS_KEYWORD = "diversos"
# Classificação geral do artigo no vocabulário do KyX (``relevante``/``ruido``/``incerto``): só o
# fato adverso é "relevante" para risco. Matéria neutra vai como ``incerto`` e o Treatment decide
# por entidade e tema; ``ruido`` a esconderia nos resultados.
VERDICT_ADVERSE = "relevante"
VERDICT_NEUTRAL = "incerto"
_ROLES = {
    "acusado": "acusado",
    "investigado": "investigado",
    "condenado": "condenado",
    "vitima": "vitima",
    "autoridade": "autoridade",
    "citado": "mencionado",
    "reu": "acusado",
}
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class CorpusEntity:
    """Entidade de um artigo (``midias_ia_article_entities``).

    Attributes:
        name: Nome como aparece na matéria.
        name_norm: Nome em ASCII, minúsculo e com espaços únicos (chave de busca do KyX).
        entity_type: ``PER`` ou ``ORG``.
        role: Papel no vocabulário do corpus (``acusado``, ``investigado``, ``mencionado``...).
        context: Fato adverso, só para papéis adversos.
    """

    name: str
    name_norm: str
    entity_type: str
    role: str
    context: str | None


@dataclass(frozen=True, slots=True)
class CorpusKeyword:
    """Palavra-chave de um artigo (``midias_ia_article_keywords``): o tema apontado.

    Attributes:
        keyword: Nome do tema em minúsculas (como o KyX grava).
        category: Slug do tema.
        field: Sempre ``analysis`` (veio da análise de contexto).
        weight: Confiança da análise.
    """

    keyword: str
    category: str
    field: str
    weight: float


@dataclass(frozen=True, slots=True)
class CorpusEvidence:
    """Estado da evidência de um artigo.

    Attributes:
        status: ``captured``, ``failed`` ou ``pending``.
        path: Caminho relativo do HTML.
        kind: ``html`` quando capturada.
        attempts: Tentativas de captura.
    """

    status: str
    path: str | None
    kind: str | None
    attempts: int


@dataclass(frozen=True, slots=True)
class CorpusArticle:
    """Um artigo pronto para o corpus (``midias_ia_articles`` e tabelas ligadas)."""

    url: str
    url_hash: str
    title: str | None
    summary: str
    domain: str | None
    source_domain: str | None
    source_name: str | None
    source_country: str | None
    source_tier: str
    language: str | None
    published_at: datetime | None
    description: str | None
    body: str | None
    ai_verdict: str
    ai_score: float
    ai_reason: str
    ai_summary: str
    ai_model: str
    first_seen_by: str
    origin: str
    evidence: CorpusEvidence
    entities: tuple[CorpusEntity, ...]
    keywords: tuple[CorpusKeyword, ...]
    content_category: str | None = None
    source_country_method: str | None = None


def normalize_entity_name(name: str) -> str:
    """Nome em ASCII, minúsculo e com espaços únicos (como o ``normalizeEntityName`` do KyX)."""
    return _WHITESPACE.sub(" ", unidecode(name).lower()).strip()


def map_entity_type(entity_type: str) -> str:
    """``PF`` vira ``PER``; empresa, órgão e o resto viram ``ORG`` (vocabulário do corpus)."""
    return "PER" if entity_type == "PF" else "ORG"


def map_role(role: str, entity_type: str) -> str:
    """Papel no vocabulário do corpus; ``responsavel`` é ``empresa_envolvida`` ou ``acusado``."""
    normalized = normalize_role(role)
    if normalized == "responsavel":
        return "empresa_envolvida" if entity_type != "PF" else "acusado"
    return _ROLES.get(normalized, "outro")


def build_corpus_article(
    candidate: CorpusCandidate,
    site: Site | None,
    source_names: Mapping[str, str],
    source_countries: Mapping[str, str] | None = None,
) -> CorpusArticle:
    """Monta o artigo do corpus a partir de uma matéria com entidade (adversa ou não).

    Args:
        candidate: Matéria com texto, análise e estado da evidência.
        site: Fonte no catálogo (dá o país); ``None`` se não for encontrada.
        source_names: Nome da fonte por domínio, das fontes confiáveis do corpus.
        source_countries: País cadastrado por domínio/seção; sem mapa usa o país de site.

    Returns:
        O artigo, com entidades (as de papel adverso primeiro e sem repetir o nome), palavras-chave
        dos temas (ou ``diversos``) e o estado da evidência.
    """
    domain = url_domain(candidate.url)
    site_country = (
        registered_country(candidate.url, source_countries)
        if source_countries is not None
        else (site.country_code if site else None)
    )
    country, method = resolve_country(candidate.language, candidate.article_country, site_country)
    return CorpusArticle(
        url=candidate.url,
        url_hash=candidate.url_hash,
        title=candidate.title,
        summary=candidate.summary,
        domain=domain,
        source_domain=domain,
        source_name=source_names.get(domain or ""),
        source_country=country,
        source_tier=SOURCE_TIER,
        language=candidate.language,
        published_at=candidate.published_at,
        description=candidate.description,
        body=candidate.lede,
        ai_verdict=VERDICT_ADVERSE if candidate.adverse else VERDICT_NEUTRAL,
        ai_score=candidate.confidence,
        ai_reason=candidate.adverse_fact,
        ai_summary=candidate.summary,
        ai_model=candidate.model,
        first_seen_by=SOURCE,
        origin=ORIGIN,
        evidence=_evidence(candidate),
        entities=_entities(candidate),
        keywords=_keywords(candidate),
        content_category=content_category(candidate.content_category),
        source_country_method=method,
    )


def _keywords(candidate: CorpusCandidate) -> tuple[CorpusKeyword, ...]:
    """Palavras-chave dos temas conhecidos; sem nenhum, a categoria ``diversos``."""
    themed = tuple(
        CorpusKeyword(THEME_KEYWORDS[theme], theme, "analysis", candidate.confidence)
        for theme in candidate.themes
        if theme in THEME_KEYWORDS
    )
    if themed:
        return themed
    return (CorpusKeyword(DIVERSOS_KEYWORD, DIVERSOS_KEYWORD, "analysis", candidate.confidence),)


def _evidence(candidate: CorpusCandidate) -> CorpusEvidence:
    """Estado da evidência no vocabulário do corpus (``pending`` sem nenhuma tentativa)."""
    if candidate.evidence_status == "captured":
        return CorpusEvidence(
            "captured", candidate.evidence_path, "html", candidate.evidence_attempts
        )
    if candidate.evidence_status == "failed":
        return CorpusEvidence("failed", None, None, candidate.evidence_attempts)
    return CorpusEvidence("pending", None, None, 0)


def _entities(candidate: CorpusCandidate) -> tuple[CorpusEntity, ...]:
    """Entidades sem repetir o nome normalizado; a de papel adverso vence a repetida."""
    ordered = sorted(candidate.entities, key=lambda e: not is_adverse_role(str(e.get("role", ""))))
    seen: dict[str, CorpusEntity] = {}
    for entity in ordered:
        name = str(entity.get("name", "")).strip()
        norm = normalize_entity_name(name)
        if not norm or norm in seen:
            continue
        kind = str(entity.get("type", ""))
        role = str(entity.get("role", ""))
        seen[norm] = CorpusEntity(
            name=name,
            name_norm=norm,
            entity_type=map_entity_type(kind),
            role=map_role(role, kind),
            context=candidate.adverse_fact if is_adverse_role(role) else None,
        )
    return tuple(seen.values())
