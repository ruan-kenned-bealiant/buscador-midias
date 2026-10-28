"""Contribuição observada e simulação retrospectiva, sem selecionar plano de coleta."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass


def exclusives(candidates: Mapping[str, set[str]]) -> dict[str, set[str]]:
    """Retorna exclusivas apenas entre os conjuntos fornecidos, nunca de todo o site."""
    occurrences = Counter(value for values in candidates.values() for value in values)
    return {
        key: {value for value in values if occurrences[value] == 1}
        for key, values in candidates.items()
    }


@dataclass(frozen=True)
class CoverageStep:
    """Passo de uma simulação gulosa sobre documentos e URLs observados."""

    mechanism: str
    added_candidates: int
    added_documents: int
    covered_candidates: int
    read_documents: int


def replay_coverage(
    candidates: Mapping[str, set[str]],
    documents: Mapping[str, set[str]],
) -> list[CoverageStep]:
    """Simula ganho por documento compartilhado; não garante solução ótima nem cobertura futura.

    Custos ausentes são rejeitados. Documentos já cobertos por uma raiz anterior não
    são cobrados novamente. Raízes sem ganho não são escolhidas nessa simulação.
    """
    remaining = {key for key, values in candidates.items() if values}
    if any(key not in documents or not documents[key] for key in remaining):
        raise ValueError("simulação requer custo documental observado para cada raiz")
    covered: set[str] = set()
    visited: set[str] = set()
    result = []
    while remaining:
        gains = {key: candidates[key] - covered for key in remaining}
        remaining = {key for key in remaining if gains[key]}
        if not remaining:
            break
        increments = {key: len(documents[key] - visited) for key in remaining}
        best = min(
            remaining,
            key=lambda key: (
                -len(gains[key]) / max(1, increments[key]),
                -len(gains[key]),
                increments[key],
                key,
            ),
        )
        covered.update(candidates[best])
        visited.update(documents[best])
        result.append(
            CoverageStep(best, len(gains[best]), increments[best], len(covered), len(visited))
        )
        remaining.remove(best)
    return result
