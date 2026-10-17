"""Regras manuais por fonte: excluir um endereço (ou um prefixo) ou forçar outro como principal.

A sondagem escolhe sozinha, e erra de vez em quando (um sitemap de tags com ``lastmod`` fresco, um
sitemap que cobre só uma editoria). Quem conhece o site corrige no CSV, e a correção vale a cada
rodada, sem mexer em código.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from buscador_midias.domain.site import Site
from buscador_midias.strategy.hints import normalize_domain, open_csv, read_rows

_ACTIONS = {"excluir": "exclude", "forcar": "force", "forçar": "force"}


@dataclass(frozen=True, slots=True)
class ManualRules:
    """O que um humano decidiu sobre os endereços de uma fonte.

    Attributes:
        exclude: Endereços que não se sondam; terminando em ``*``, vale o prefixo.
        force: Endereços a usar como principal (se servirem: um que está fora do ar não vale).
    """

    exclude: tuple[str, ...]
    force: tuple[str, ...]


class ManualRulesBook:
    """As regras manuais de todas as fontes, por chave."""

    def __init__(self, rules: dict[str, ManualRules]) -> None:
        """Guarda as regras por chave de fonte."""
        self._rules = rules

    def for_site(self, site: Site) -> ManualRules:
        """As regras da fonte; um endereço forçado de outro domínio é descartado."""
        found = self._rules.get(site.key, ManualRules((), ()))
        return ManualRules(found.exclude, tuple(u for u in found.force if site.owns_host(u)))


def is_excluded(url: str, rules: Sequence[str]) -> bool:
    """Se uma regra manda não sondar o endereço (``*`` no fim vale o prefixo)."""
    return any(url.startswith(rule[:-1]) if rule.endswith("*") else url == rule for rule in rules)


def load_manual_rules(path: Path) -> ManualRulesBook:
    """Lê o CSV (``dominio``, ``endereco``, ``acao``, ``motivo``); arquivo ausente = sem regras.

    Args:
        path: O CSV. ``acao`` é ``excluir`` ou ``forçar`` (com ou sem acento, em qualquer caixa).

    Returns:
        As regras por fonte.

    Raises:
        ValueError: Se uma linha tem ação desconhecida (erro de digitação que, ignorado, deixaria
            a correção sem efeito sem ninguém notar).
    """
    exclude: dict[str, list[str]] = {}
    force: dict[str, list[str]] = {}
    if path.is_file():
        with open_csv(path) as handle:
            for line, row in enumerate(read_rows(handle), start=2):
                action = _ACTIONS.get((row.get("acao") or "").lower())
                url = row.get("endereco") or ""
                if not url:
                    continue
                if action is None:
                    raise ValueError(f"{path}: linha {line}: ação desconhecida {row.get('acao')!r}")
                key = normalize_domain(row.get("dominio") or "")
                (exclude if action == "exclude" else force).setdefault(key, []).append(url)
    keys = set(exclude) | set(force)
    return ManualRulesBook(
        {k: ManualRules(tuple(exclude.get(k, ())), tuple(force.get(k, ()))) for k in keys}
    )
