"""Vocabulário dos vereditos humanos, compartilhado pela pontuação e pela tela de rotulagem.

A pergunta da rotulagem é: "esta notícia entra no corpus?", isto é, é uma matéria que nomeia ao
menos uma pessoa ou organização identificável (boa, ruim ou neutra). Não é "é adversa?".
"""

from __future__ import annotations

import unicodedata

YES = "yes"
NO = "no"
UNCERTAIN = "uncertain"
UNLABELED = "unlabeled"

# Palavra gravada no CSV para cada veredito (a que a pessoa digitaria à mão).
CSV_WORDS: dict[str, str] = {
    YES: "sim",
    NO: "nao",
    UNCERTAIN: "incerto",
    UNLABELED: "",
}

# Só o vocabulário novo vale: "adverso"/"nao_adverso" respondiam a outra pergunta e, lidos como
# sim/não, trocariam o sentido do rótulo em silêncio (viram "sem rótulo").
_YES = frozenset({"sim", "s", "1", "yes", "y", "entra"})
_NO = frozenset({"nao", "n", "0", "no", "nao entra"})
_UNCERTAIN = frozenset({"incerto", "talvez", "?", "duvida"})


def fold(text: str) -> str:
    """Minúsculas, sem acento e sem espaços nas pontas."""
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def parse_verdict(raw: str) -> str:
    """Normaliza o veredito escrito à mão (sinônimos, acento, caixa).

    Args:
        raw: Texto da coluna ``veredito``.

    Returns:
        ``yes``, ``no``, ``uncertain`` ou ``unlabeled`` (vazio ou desconhecido).
    """
    value = fold(raw)
    if value in _YES:
        return YES
    if value in _NO:
        return NO
    if value in _UNCERTAIN:
        return UNCERTAIN
    return UNLABELED
