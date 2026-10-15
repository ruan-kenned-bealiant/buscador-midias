"""Exporta o catálogo de fontes confiáveis do seed do powerdd-backend para ``data/sites.csv``.

Uso:
    python scripts/export_sites.py [caminho-do-seed.sql] [caminho-do-csv]

O CSV é uma exportação fiel (inclui duplicatas); a deduplicação é feita por ``load_sites``.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from buscador_midias.sites.loader import REQUIRED_COLUMNS, load_sites
from buscador_midias.sites.seed_import import parse_trusted_sources_sql

DEFAULT_SEED = (
    Path(__file__).resolve().parents[2]
    / "powerdd-backend/database/seeders/data/midias-ia/midias_ia_trusted_sources.sql"
)
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/sites.csv"


def main(argv: list[str]) -> int:
    """Converte o seed SQL em CSV e imprime a contagem de fontes, duplicatas e seções.

    Args:
        argv: Argumentos opcionais: caminho do seed e caminho do CSV de saída.

    Returns:
        Código de saída do processo (0 em sucesso).
    """
    seed = Path(argv[0]) if argv else DEFAULT_SEED
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT

    rows = parse_trusted_sources_sql(seed.read_text(encoding="utf-8"))

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    result = load_sites(output)
    sections = sum(1 for site in result.sites if site.path_prefix)
    print(f"linhas exportadas: {len(rows)}")
    print(f"fontes únicas:     {len(result.sites)}")
    print(f"duplicatas:        {len(result.duplicates)}")
    print(f"fontes de seção:   {sections}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
