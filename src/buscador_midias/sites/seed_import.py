"""Extração das fontes confiáveis a partir do seed SQL do powerdd-backend."""

from __future__ import annotations

import re

_QUOTED = r"'((?:[^']|'')*)'"
_QUOTED_OR_NULL = r"(?:'(?:[^']|'')*'|NULL)"

# Ordem das colunas no INSERT: region, name, url, url_without_https_www, country, country_code,
# headquarters (anulável), source_type, language, ...
_ROW = re.compile(
    rf"^\({_QUOTED}, {_QUOTED}, {_QUOTED}, {_QUOTED}, {_QUOTED}, {_QUOTED}, "
    rf"{_QUOTED_OR_NULL}, {_QUOTED}, {_QUOTED},",
    re.MULTILINE,
)
_ROW_START = re.compile(r"^\('", re.MULTILINE)


def parse_trusted_sources_sql(sql: str) -> list[dict[str, str]]:
    """Extrai de cada linha do INSERT os campos usados pelo crawler.

    Falha alto se alguma linha do INSERT não for reconhecida, para que uma mudança de formato do
    seed nunca resulte em fontes perdidas em silêncio.

    Args:
        sql: Conteúdo do arquivo ``midias_ia_trusted_sources.sql``.

    Returns:
        Uma lista de dicionários com ``region``, ``name``, ``url``, ``country``, ``country_code``,
        ``language`` e ``source_type``.

    Raises:
        ValueError: Se o número de linhas reconhecidas diferir do número de linhas do INSERT.
    """
    matches = list(_ROW.finditer(sql))
    expected = len(_ROW_START.findall(sql))
    if len(matches) != expected:
        raise ValueError(f"{len(matches)} linhas reconhecidas de {expected} no seed")

    return [
        {
            "region": _unescape(m[1]),
            "name": _unescape(m[2]),
            "url": _unescape(m[3]),
            "country": _unescape(m[5]),
            "country_code": _unescape(m[6]),
            "language": _unescape(m[8]),
            "source_type": _unescape(m[7]),
        }
        for m in matches
    ]


def _unescape(value: str) -> str:
    """Desfaz o escape SQL de aspas simples (``''`` vira ``'``)."""
    return value.replace("''", "'")
