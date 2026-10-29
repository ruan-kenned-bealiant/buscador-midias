"""Registros que atravessam as etapas da notícia: achado, evidência e candidato ao corpus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from buscador_midias.analysis.entity_context import EntityContext


@dataclass(frozen=True, slots=True)
class FindingUrl:
    """URL (achado adverso ou matéria com entidade) que espera a captura da evidência.

    Attributes:
        url_hash: Hash canônico.
        url: URL.
        title: Título do news sitemap, se houve.
        site_key: Fonte.
        entity_names: Entidades que a evidência precisa mencionar (ao menos uma): as de papel
            adverso no achado adverso; qualquer uma nas demais matérias.
        adverse: Se a análise apontou fato adverso. Só estes justificam pagar pelo Bright Data.
    """

    url_hash: str
    url: str
    title: str | None
    site_key: str
    entity_names: tuple[str, ...]
    adverse: bool = False


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Estado da captura da evidência de uma URL.

    Attributes:
        url_hash: Hash canônico.
        status: ``captured`` ou ``failed``.
        provider: ``direct`` (HTML reaproveitado) ou ``brightdata``.
        path: Caminho relativo do HTML no armazenamento.
        sha256: Hash do conteúdo gravado (integridade).
        size_bytes: Tamanho gravado.
        content_type: Tipo de conteúdo recebido.
        http_status: Status HTTP da fonte do conteúdo.
        final_url: URL final da fonte do conteúdo.
        rejected_reasons: Por que o HTML do download direto não foi reaproveitado (se não foi).
        error: Falha da captura, quando ``failed``.
        attempts: Tentativas feitas (preenchido na leitura).
    """

    url_hash: str
    status: str
    provider: str | None
    path: str | None
    sha256: str | None
    size_bytes: int
    content_type: str | None
    http_status: int | None
    final_url: str | None
    rejected_reasons: tuple[str, ...]
    error: str | None
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class CorpusCandidate:
    """Um achado pronto para ser gravado no corpus de Mídias IA.

    Attributes:
        url_hash: Hash canônico.
        url: URL da matéria.
        site_key: Fonte (``Site.key``).
        title: Título (da página ou do sitemap).
        description: Descrição da página.
        lede: Início do texto extraído.
        published_at: Data de publicação extraída do HTML.
        language: Idioma extraído do HTML.
        themes: Temas apontados.
        entities: Entidades (``name``, ``type``, ``role``).
        adverse_fact: Fato adverso em uma frase.
        summary: Resumo curto.
        confidence: Confiança da análise.
        basis: ``text`` ou ``headline``.
        model: Modelo que analisou.
        evidence_status: ``captured``/``failed``, ou ``None`` sem tentativa.
        evidence_path: Caminho relativo da evidência.
        evidence_attempts: Tentativas de captura.
        evidence_provider: ``direct`` ou ``brightdata``.
        adverse: Se a análise apontou fato adverso (senão é uma matéria só com entidade).
    """

    url_hash: str
    url: str
    site_key: str
    title: str | None
    description: str | None
    lede: str | None
    published_at: datetime | None
    language: str | None
    themes: tuple[str, ...]
    entities: tuple[dict[str, str], ...]
    adverse_fact: str
    summary: str
    confidence: float
    basis: str
    model: str
    evidence_status: str | None
    evidence_path: str | None
    evidence_attempts: int
    evidence_provider: str | None
    adverse: bool
    content_category: str | None = None
    article_country: str | None = None
    entity_contexts: tuple[EntityContext, ...] = ()
    context_prompt_version: str | None = None
