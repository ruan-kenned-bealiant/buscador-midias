"""Metadados dos mecanismos, mantidos como fachada para os consumidores existentes."""

from typing import Any

from buscador_midias.catalog.mechanisms import MECHANISMS, get_mechanism


def get_model(value: object, kind: str | None = None) -> dict[str, Any]:
    """Valida o modelo e fornece seus metadados e parâmetros padrão."""
    return get_mechanism(value, kind).descriptor()


def list_models() -> list[dict[str, Any]]:
    """Expõe as definições dos cinco mecanismos implementados em arquivos separados."""
    return [m.descriptor() for m in MECHANISMS.values()]
