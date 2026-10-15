"""Análise de contexto: contrato, leitura defensiva da resposta e adaptador do OpenRouter."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from buscador_midias.analysis.prompt import SYSTEM_PROMPT
from buscador_midias.domain.roles import is_adverse_role, normalize_role
from buscador_midias.triage.headline import tokens
from buscador_midias.triage.openrouter import OpenRouterChat, Usage
from buscador_midias.triage.relevance import THEME_SLUGS, json_items

DEFAULT_BATCH_SIZE = 6
DEFAULT_MAX_TEXT_CHARS = 1500
HEADLINE_ONLY_MAX_CONFIDENCE = 0.6
_MAX_THEMES = 3
_MAX_ENTITIES = 6
_MAX_NAME = 120
_MAX_ROLE = 30
_MAX_FACT = 300
_MAX_SUMMARY = 400
_TOKENS_PER_ITEM = 380
_BASE_TOKENS = 300
_ENTITY_TYPES = frozenset({"PF", "PJ", "OUTRO"})
_MIN_NAME_WORD = 3
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AnalysisItem:
    """Uma matéria a analisar.

    Attributes:
        id: Identificador dentro do lote (volta no veredito).
        domain: Domínio da fonte.
        headline: Título (do news sitemap, da página ou o slug).
        description: Descrição da página (pode ser vazia).
        text: Início do texto do artigo (vazio quando não foi possível baixar).
        basis: ``text`` (há texto) ou ``headline`` (só a manchete).
    """

    id: int
    domain: str
    headline: str
    description: str
    text: str
    basis: str


@dataclass(frozen=True, slots=True)
class ContextVerdict:
    """Resultado da análise de uma matéria.

    Attributes:
        id: ``AnalysisItem.id``.
        decided: ``False`` quando o modelo não respondeu de forma aproveitável (continua pendente).
        finding: Há fato adverso envolvendo pessoa ou empresa nomeada.
        themes: Temas apontados (até 3).
        entities: Entidades citadas (``name``, ``type``, ``role``).
        adverse_fact: Fato adverso em uma frase.
        summary: Resumo curto.
        confidence: Confiança (0 a 1; no máximo 0,6 quando só havia a manchete).
        basis: ``text`` ou ``headline`` (vem do item, não do modelo).
    """

    id: int
    decided: bool
    finding: bool
    themes: tuple[str, ...]
    entities: tuple[dict[str, str], ...]
    adverse_fact: str
    summary: str
    confidence: float
    basis: str


class ContextAnalyzer(Protocol):
    """Porta: analisa um lote de matérias."""

    def analyze(self, items: Sequence[AnalysisItem]) -> list[ContextVerdict]:
        """Devolve um veredito por item, na mesma ordem."""
        ...


def build_user_message(
    items: Sequence[AnalysisItem], *, max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
) -> str:
    """Mensagem do usuário: uma matéria por linha, em JSON.

    Args:
        items: Matérias do lote.
        max_text_chars: Limite do texto enviado de cada uma (controla o custo em tokens).

    Returns:
        O texto a enviar ao modelo.
    """
    lines = []
    for item in items:
        lines.append(
            json.dumps(
                {
                    "id": item.id,
                    "fonte": item.domain,
                    "manchete": item.headline,
                    "descricao": item.description,
                    "texto": item.text[:max_text_chars],
                    "base": "texto" if item.basis == "text" else "somente_manchete",
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def parse_analysis(text: str, items: Sequence[AnalysisItem]) -> list[ContextVerdict]:
    """Interpreta a resposta do modelo: um veredito por item, na ordem.

    Defensiva: aceita JSON em bloco de código e ids como texto; o que faltar ou vier inválido
    vira um veredito indeciso (o item segue pendente). Um achado sem entidade nomeada não conta
    como achado. A ``basis`` vem do item, e com só a manchete a confiança é limitada a 0,6.

    Args:
        text: Conteúdo devolvido pelo modelo.
        items: Matérias do lote.

    Returns:
        Os vereditos, na ordem de ``items``.
    """
    parsed: dict[int, dict[str, Any]] = {}
    for entry in json_items(text):
        item_id = _int(entry.get("id")) if isinstance(entry, dict) else None
        if item_id is not None and "finding" in entry:
            parsed.setdefault(item_id, entry)
    return [_verdict(item, parsed.get(item.id)) for item in items]


def analyze_all(
    analyzer: ContextAnalyzer,
    items: Sequence[AnalysisItem],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    workers: int = 4,
) -> list[ContextVerdict]:
    """Analisa muitas matérias em lotes paralelos, preservando a ordem.

    Um lote que levanta exceção vira indeciso para os seus itens e não afeta os demais.

    Args:
        analyzer: Porta de análise (thread-safe).
        items: Matérias a analisar.
        batch_size: Matérias por chamada.
        workers: Chamadas simultâneas.

    Returns:
        Um veredito por item, na ordem de entrada.
    """
    batches = [items[start : start + batch_size] for start in range(0, len(items), batch_size)]

    def run(batch: Sequence[AnalysisItem]) -> list[ContextVerdict]:
        try:
            return analyzer.analyze(batch)
        except Exception as error:  # noqa: BLE001 - isola o lote que falhou
            _LOG.warning("lote de %d matérias falhou: %s", len(batch), type(error).__name__)
            return [_undecided(item) for item in batch]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, batches))
    return [verdict for batch in results for verdict in batch]


class OpenRouterContextAnalyzer:
    """Pede a um modelo do OpenRouter a análise de contexto de cada matéria do lote."""

    def __init__(
        self,
        chat: OpenRouterChat,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    ) -> None:
        """Cria o analisador.

        Args:
            chat: Transporte do OpenRouter.
            system_prompt: Prompt de sistema.
            max_text_chars: Limite do texto de cada matéria enviado ao modelo.
        """
        self._chat = chat
        self._system_prompt = system_prompt
        self._max_text_chars = max_text_chars

    @property
    def model(self) -> str:
        """Modelo em uso."""
        return self._chat.model

    @property
    def usage(self) -> Usage:
        """Consumo acumulado."""
        return self._chat.usage

    def analyze(self, items: Sequence[AnalysisItem]) -> list[ContextVerdict]:
        """Analisa um lote; falha do provedor deixa todos indecisos (e pendentes)."""
        content = self._chat.complete(
            self._system_prompt,
            build_user_message(items, max_text_chars=self._max_text_chars),
            max_tokens=len(items) * _TOKENS_PER_ITEM + _BASE_TOKENS,
        )
        if content is None:
            return [_undecided(item) for item in items]
        return parse_analysis(content, items)


def _undecided(item: AnalysisItem) -> ContextVerdict:
    """Veredito de um item que o modelo não respondeu."""
    return ContextVerdict(item.id, False, False, (), (), "", "", 0.0, item.basis)


def _int(value: Any) -> int | None:
    """Inteiro de um valor numérico ou textual, ou ``None``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _verdict(item: AnalysisItem, entry: dict[str, Any] | None) -> ContextVerdict:
    """Veredito de um item a partir da entrada da resposta (ou indeciso se não houver)."""
    if entry is None:
        return _undecided(item)
    entities = _entities(entry.get("entities"), item)
    finding = entry.get("finding") is True and any(is_adverse_role(e["role"]) for e in entities)
    confidence = _confidence(entry.get("confidence"))
    if item.basis != "text":
        confidence = min(confidence, HEADLINE_ONLY_MAX_CONFIDENCE)
    raw_themes = entry.get("themes")
    themes = tuple(
        t for t in (raw_themes if isinstance(raw_themes, list) else []) if t in THEME_SLUGS
    )
    return ContextVerdict(
        id=item.id,
        decided=True,
        finding=finding,
        themes=themes[:_MAX_THEMES],
        entities=entities,
        adverse_fact=str(entry.get("adverse_fact", ""))[:_MAX_FACT],
        summary=str(entry.get("summary", ""))[:_MAX_SUMMARY],
        confidence=confidence,
        basis=item.basis,
    )


def _entities(raw: Any, item: AnalysisItem) -> tuple[dict[str, str], ...]:
    """Entidades saneadas e ancoradas no texto fornecido ao modelo.

    Nome obrigatório, tipo conhecido, papel curto sem acento. Uma entidade cujo nome não aparece no
    que o modelo recebeu (manchete, descrição e texto) foi inventada ou parafraseada e é descartada:
    sem isso a matéria poderia virar achado por causa de um nome que não consta nela.
    """
    clean: list[dict[str, str]] = []
    available = set(tokens(f"{item.headline} {item.description} {item.text}"))
    for entity in raw if isinstance(raw, list) else []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name", "")).strip()[:_MAX_NAME]
        if not name or not _grounded(name, available):
            continue
        kind = str(entity.get("type", "")).strip().upper()
        role = normalize_role(str(entity.get("role", "")))[:_MAX_ROLE] or "outro"
        clean.append(
            {"name": name, "type": kind if kind in _ENTITY_TYPES else "OUTRO", "role": role}
        )
    return tuple(clean[:_MAX_ENTITIES])


def _grounded(name: str, available: set[str]) -> bool:
    """Se todas as palavras do nome (com 3+ letras) aparecem no texto, sem acento nem caixa."""
    words = [word for word in tokens(name) if len(word) >= _MIN_NAME_WORD]
    return bool(words) and all(word in available for word in words)


def _confidence(value: Any) -> float:
    """Confiança limitada ao intervalo [0, 1]; valor não numérico vira 0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(number, 0.0), 1.0)
