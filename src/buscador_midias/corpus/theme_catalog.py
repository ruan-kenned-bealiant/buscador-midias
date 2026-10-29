"""Catálogo de temas do KyX ligado aos slugs da análise de contexto.

O front (PowerDD/KyX) identifica o tema pelo ``theme_id`` do KyX (tabela ``temas``), que muda com o
tipo de pessoa: ``person_type`` 1 = PF nacional, 2 = PJ nacional, 3 = PF estrangeira, 4 = PJ
estrangeira. Uma menção só traz o nome, então cada tema é gravado nas duas variantes do tipo
(nacional e estrangeira) e o front exibe a que casa com a busca. Os dados ficam em
``theme_catalog.json``, versionados: mudou o catálogo, mude o arquivo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources


@dataclass(frozen=True, slots=True)
class ThemeRef:
    """Linha do catálogo de temas: o que vai em ``theme_id``, ``theme`` e ``theme_description``."""

    theme_id: int
    name: str
    description: str


@cache
def _catalog() -> dict[str, dict[str, tuple[ThemeRef, ...]]]:
    """Lê o JSON uma vez e resolve os ids de cada slug em ``ThemeRef``."""
    raw = json.loads(
        resources.files("buscador_midias.corpus").joinpath("theme_catalog.json").read_text("utf-8")
    )
    themes = {
        int(i): ThemeRef(int(i), t["name"], t["description"]) for i, t in raw["themes"].items()
    }
    return {
        slug: {kind: tuple(themes[i] for i in ids) for kind, ids in kinds.items()}
        for slug, kinds in raw["map"].items()
    }


def theme_refs(slug: str, entity_type: str) -> tuple[ThemeRef, ...]:
    """Temas do catálogo para o slug e o tipo da entidade; ``OUTRO`` segue como pessoa jurídica."""
    kind = "pf" if entity_type == "PF" else "pj"
    return _catalog().get(slug, {}).get(kind, ())


@cache
def _slugs() -> dict[int, str]:
    """Cada ``theme_id`` do KyX com o slug da análise a que pertence."""
    return {
        ref.theme_id: slug
        for slug, kinds in _catalog().items()
        for refs in kinds.values()
        for ref in refs
    }


def slug_of(theme_id: int) -> str | None:
    """O slug da análise de um ``theme_id`` do KyX, seja PF, PJ, nacional ou estrangeira.

    O tema da análise é o slug (``midias_ia_theme_prompts``); a variante por tipo de pessoa só
    existe nos ids do KyX. ``None`` para um id fora do catálogo.
    """
    return _slugs().get(theme_id)
