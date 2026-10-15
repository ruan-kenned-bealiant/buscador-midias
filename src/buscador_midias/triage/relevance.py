"""Contrato da triagem de relevância por manchete: itens, vereditos e leitura da resposta do LLM."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

DEFAULT_BATCH_SIZE = 25
_MAX_THEMES = 3
_MAX_REASON = 200
_LOG = logging.getLogger(__name__)

# Temas aceitos na resposta do modelo (slugs de ``midias_ia_theme_prompts``; "imagem" vai em
# "reputacional"; cadastral, bens e entidades associadas não decidem relevância sozinhos).
THEME_SLUGS: tuple[str, ...] = (
    "corrupcao",
    "financeiro",
    "trabalhista",
    "ambiental",
    "fiscal",
    "criminal",
    "reputacional",
    "etica_integridade",
    "pldft",
    "processos_judiciais",
    "ciber_seguranca",
)


class Decision(StrEnum):
    """Decisão sobre uma manchete."""

    KEEP = "keep"
    DISCARD = "discard"
    UNKNOWN = "unknown"  # sem veredito confiável: nunca é motivo para descartar


@dataclass(frozen=True, slots=True)
class HeadlineItem:
    """Uma manchete a triar.

    Attributes:
        id: Identificador dentro do lote (volta no veredito).
        domain: Domínio da fonte (contexto para o modelo).
        text: Título ou slug da URL.
    """

    id: int
    domain: str
    text: str


@dataclass(frozen=True, slots=True)
class HeadlineVerdict:
    """Veredito sobre uma manchete.

    Attributes:
        id: ``HeadlineItem.id``.
        decision: ``KEEP``, ``DISCARD`` ou ``UNKNOWN``.
        themes: Temas adversos citados (slugs conhecidos, no máximo 3).
        reason: Justificativa curta do modelo (auditoria).
    """

    id: int
    decision: Decision
    themes: tuple[str, ...]
    reason: str


class HeadlineClassifier(Protocol):
    """Porta: decide, por lote de manchetes, o que segue adiante."""

    def classify(self, items: Sequence[HeadlineItem]) -> list[HeadlineVerdict]:
        """Devolve um veredito por item, na mesma ordem."""
        ...


def parse_verdicts(text: str, items: Sequence[HeadlineItem]) -> list[HeadlineVerdict]:
    """Interpreta a resposta do modelo, sempre devolvendo um veredito por item e na ordem.

    É defensiva: aceita JSON dentro de bloco de código, ids como texto e decisão em qualquer caixa.
    O que faltar, repetir ou vier inválido não derruba nada: o item fica ``UNKNOWN`` (nunca
    descartado). Ids que não pertencem ao lote são ignorados e, num id repetido, vale o primeiro.

    Args:
        text: Conteúdo devolvido pelo modelo.
        items: Manchetes do lote, que definem os ids esperados.

    Returns:
        Os vereditos, na ordem de ``items``.
    """
    parsed: dict[int, HeadlineVerdict] = {}
    for entry in json_items(text):
        verdict = _verdict(entry, THEME_SLUGS)
        if verdict is not None:
            parsed.setdefault(verdict.id, verdict)
    return [
        parsed.get(item.id) or HeadlineVerdict(item.id, Decision.UNKNOWN, (), "sem veredito")
        for item in items
    ]


def classify_all(
    classifier: HeadlineClassifier,
    items: Sequence[HeadlineItem],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    workers: int = 4,
) -> list[HeadlineVerdict]:
    """Classifica muitas manchetes em lotes paralelos, preservando a ordem.

    Um lote que levanta exceção vira ``UNKNOWN`` para os seus itens e não afeta os demais.

    Args:
        classifier: Porta de classificação (deve ser thread-safe).
        items: Manchetes a triar.
        batch_size: Manchetes por chamada.
        workers: Chamadas simultâneas.

    Returns:
        Um veredito por item, na ordem de entrada.
    """
    batches = [items[start : start + batch_size] for start in range(0, len(items), batch_size)]

    def run(batch: Sequence[HeadlineItem]) -> list[HeadlineVerdict]:
        try:
            return classifier.classify(batch)
        except Exception as error:  # noqa: BLE001 - isola o lote que falhou
            _LOG.warning("lote de %d manchetes falhou: %s", len(batch), type(error).__name__)
            return [HeadlineVerdict(i.id, Decision.UNKNOWN, (), "falha no lote") for i in batch]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, batches))
    return [verdict for batch in results for verdict in batch]


def json_items(text: str) -> list[Any]:
    """Lista de itens do JSON da resposta de um modelo (vazia se não for interpretável).

    Aceita ``{"items": [...]}`` e também um array puro ``[...]`` na raiz (alguns modelos ignoram o
    objeto pedido), com ou sem bloco de código ou texto em volta.
    """
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        entries = payload.get("items") if isinstance(payload, dict) else payload
        if isinstance(entries, list):
            return entries
    return []


def _json_candidates(text: str) -> Iterator[str]:
    """Trechos que podem ser o JSON da resposta: o texto todo, o objeto e o array internos."""
    yield text.strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            yield text[start : end + 1]


def _verdict(entry: Any, known_themes: Sequence[str]) -> HeadlineVerdict | None:
    """Veredito de uma entrada da resposta, ou ``None`` se não dá para aproveitar."""
    if not isinstance(entry, dict):
        return None
    try:
        item_id = int(entry["id"])
        decision = Decision(str(entry["decision"]).strip().lower())
    except (KeyError, ValueError, TypeError):
        return None
    if decision is Decision.UNKNOWN:
        return None
    raw_themes = entry.get("themes")
    themes = tuple(
        theme
        for theme in (raw_themes if isinstance(raw_themes, list) else [])
        if theme in known_themes
    )[:_MAX_THEMES]
    return HeadlineVerdict(item_id, decision, themes, str(entry.get("reason", ""))[:_MAX_REASON])
