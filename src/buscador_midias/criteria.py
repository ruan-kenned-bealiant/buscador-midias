"""Critério do corpus: o que uma matéria precisa ter para entrar.

O projeto busca matérias **com entidade identificável**, adversas ou não. Uma matéria atende ao
critério quando:

1. a análise leu o texto dela (``basis == "text"``): só pela manchete a análise é pouco confiável,
   então essas ficam de fora; e
2. a análise achou ao menos uma pessoa ou organização citada pelo nome no texto.

É a única regra de "entra ou não": a captura de evidência e a gravação no corpus olham só para ela.
Ela existe em duas formas que precisam concordar (há um teste para isso): em Python, para decidir
sobre uma análise em memória, e em SQL, para as consultas sobre ``url_analysis a``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

BASIS_TEXT = "text"

# ``entities`` guarda só nomes que constam no texto analisado; lista vazia = sem entidade.
HAS_ENTITIES_SQL = "a.entities NOT IN ('[]', '')"
MEETS_CRITERIA_SQL = f"(a.basis = '{BASIS_TEXT}' AND {HAS_ENTITIES_SQL})"


def has_entities(entities: Sequence[Mapping[str, str]]) -> bool:
    """Se há ao menos uma entidade com nome."""
    return any(entity.get("name") for entity in entities)


def meets_criteria(*, basis: str, entities: Sequence[Mapping[str, str]]) -> bool:
    """Se a análise de uma matéria atende ao critério do corpus.

    Args:
        basis: ``text`` (a análise leu a matéria) ou ``headline`` (só a manchete).
        entities: Entidades que a análise apontou (``name``, ``type``, ``role``).

    Returns:
        ``True`` se a matéria deve receber evidência e ir para o corpus.
    """
    return basis == BASIS_TEXT and has_entities(entities)
