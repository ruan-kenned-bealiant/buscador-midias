"""Leitura tolerante dos CSVs de rotulagem (a pessoa pode ter salvado pelo Excel)."""

from __future__ import annotations

import csv
import io
from pathlib import Path


def read_rows(path: Path) -> list[dict[str, str]]:
    """Linhas de um CSV tolerando UTF-8 com BOM, Windows-1252 e ``;`` do Excel em português.

    As colunas voltam com o nome em minúsculas e sem espaços nas pontas, na ordem do cabeçalho.

    Args:
        path: Arquivo CSV.

    Returns:
        Uma ``dict`` por linha de dados.
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    header = text.splitlines()[0] if text else ""
    delimiter = ";" if header.count(";") > header.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [{(k or "").strip().lower(): (v or "") for k, v in row.items()} for row in reader]
