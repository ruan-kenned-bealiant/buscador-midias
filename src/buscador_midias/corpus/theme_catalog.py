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
