"""Mecanismo automático: reconhece formatos e percorre índices quando presentes."""

from buscador_midias.catalog.mechanisms.base import COMMON_PARAMETERS, TREE_PARAMETERS, Mechanism
from buscador_midias.catalog.mechanisms.parsing import Document


class AutomaticMechanism(Mechanism):
    """Detecta o formato sem exigir que o usuário conheça a estrutura do XML."""

    id = "auto"
    label = "Automático"
    description = "Identifica RSS/Atom, sitemap direto ou índice e segue seus filhos."
    kind = None
    expand = True
    parameters = COMMON_PARAMETERS + TREE_PARAMETERS

    def accepts(self, document: Document) -> bool:
        """Aceita os formatos reconhecidos pelo parser defensivo."""
        return document["kind"] in ("feed", "sitemap")

    def children(self, document: Document) -> list[str]:
        """Segue apenas referências declaradas pelo índice."""
        return document["children"] if document["index"] else []

    def requested_kind(self, declared: str, document: Document) -> str:
        """Ajusta o tipo ao formato efetivamente lido."""
        return document["kind"]
