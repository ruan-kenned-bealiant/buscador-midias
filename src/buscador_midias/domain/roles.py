"""Papéis de uma entidade em uma matéria e quais deles tornam a notícia adversa."""

from __future__ import annotations

import unicodedata

# Quem é apontado como acusado, investigado, condenado, réu ou responsável por um fato adverso.
# Autoridade, vítima, testemunha e "citado" aparecem na matéria, mas não a tornam adversa.
ADVERSE_ROLES: frozenset[str] = frozenset(
    {"acusado", "investigado", "condenado", "reu", "responsavel"}
)


def normalize_role(role: str) -> str:
    """Papel em minúsculas e sem acento (``"Réu"`` vira ``"reu"``)."""
    decomposed = unicodedata.normalize("NFKD", role.strip().lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def is_adverse_role(role: str) -> bool:
    """Indica se o papel aponta quem é alvo do fato adverso."""
    return normalize_role(role) in ADVERSE_ROLES
