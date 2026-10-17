"""Leitura de um plano explícito de mecanismos validados, sem alterar estratégias no banco."""

import json
from pathlib import Path

from buscador_midias.domain.site import Site
from buscador_midias.domain.strategy import (
    CandidateOrigin,
    CandidateReport,
    Role,
    StrategyKind,
    Verdict,
)
from buscador_midias.domain.url import canonicalize_url
from buscador_midias.strategy.hints import _is_signed


def load_discovery_plan(path: Path, sites: dict[str, Site]) -> dict[str, list[CandidateReport]]:
    """Lê o formato do piloto: chave e mecanismos (url/kind), principal seguido de complementos.

    Rejeita fontes desconhecidas, hosts externos, links assinados, tipos inválidos e
    planos vazios/repetidos. A aplicação é explícita e não grava escolhas no banco.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("o plano precisa ser uma lista de fontes")
        result: dict[str, list[CandidateReport]] = {}
        for row in data:
            key = row["chave"]
            if key not in sites or key in result:
                raise ValueError(f"fonte desconhecida ou repetida: {key}")
            members = row["mecanismos"]
            if not isinstance(members, list) or not members:
                raise ValueError(f"fonte sem mecanismos: {key}")
            reports: list[CandidateReport] = []
            seen: set[str] = set()
            for item in members:
                url = item["url"]
                kind = StrategyKind(item["kind"])
                if (not isinstance(url, str) or kind is StrategyKind.LISTING
                        or not sites[key].owns_host(url) or _is_signed(url)
                        or canonicalize_url(url) in seen):
                    raise ValueError(f"mecanismo inválido, externo ou repetido: {key}")
                seen.add(canonicalize_url(url))
                reports.append(CandidateReport(
                    url, CandidateOrigin.HINT, Verdict.FRESH, kind, None, 0,
                    "plano explícito; sujeito à conferência HTTP e ao escopo da fonte",
                    role=Role.PRIMARY if not reports else Role.COMPLEMENT,
                ))
            result[key] = reports
        return result
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"plano de descoberta inválido: {path}: {error}") from error
