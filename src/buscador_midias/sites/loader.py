"""Carga e deduplicação do catálogo de fontes a partir do CSV versionado."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from buscador_midias.domain.site import Site

REQUIRED_COLUMNS = (
    "region",
    "name",
    "url",
    "country",
    "country_code",
    "language",
    "source_type",
)


@dataclass(frozen=True, slots=True)
class LoadResult:
    """Resultado da carga do catálogo.

    Attributes:
        sites: Fontes únicas, na ordem do arquivo (a primeira ocorrência vence).
        duplicates: Fontes descartadas por repetirem a chave de uma já carregada.
    """

    sites: tuple[Site, ...]
    duplicates: tuple[Site, ...]


def load_sites(path: Path) -> LoadResult:
    """Lê o CSV de fontes, normaliza as URLs e remove duplicatas.

    Duas linhas são a mesma fonte quando têm o mesmo ``Site.key`` (host sem ``www`` + prefixo de
    seção). Um domínio inteiro e uma seção dele são fontes distintas.

    Args:
        path: Caminho do CSV, com as colunas de ``REQUIRED_COLUMNS``.

    Returns:
        As fontes únicas e as duplicatas descartadas.

    Raises:
        ValueError: Se faltarem colunas ou se alguma URL for inválida (informa a linha).
    """
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"colunas ausentes em {path.name}: {', '.join(missing)}")

        unique: dict[str, Site] = {}
        duplicates: list[Site] = []
        for row in reader:
            site = _build_site(row, line=reader.line_num)
            if site.key in unique:
                duplicates.append(site)
            else:
                unique[site.key] = site

    return LoadResult(sites=tuple(unique.values()), duplicates=tuple(duplicates))


def _build_site(row: dict[str, str], *, line: int) -> Site:
    """Converte uma linha do CSV em ``Site``, anexando o número da linha ao erro."""
    try:
        return Site.create(**{column: row[column] for column in REQUIRED_COLUMNS})
    except ValueError as error:
        raise ValueError(f"linha {line}: {error}") from error
