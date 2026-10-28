"""Aplica ao catálogo as propostas da rodada de mapeamento, em duas etapas retomáveis.

``vet`` relê cada candidato ao vivo (tema útil, notícia no último mês) e grava ``vetados.json``
sem tocar no catálogo. ``apply`` cria como ativo, pelo próprio cadastro e com a prova do teste,
só o que a revalidação manteve.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from buscador_midias.catalog.mapping_apply import apply_mapping, vet_candidates
from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.catalog.review_cli import _real_reader

OUT = Path(__file__).resolve().parents[1] / "var/maintenance/mapeamento/rodada-principais"


def candidates() -> list[dict]:
    """Cada proposta com a prova (resultado do teste) que a gerou."""
    wanted = {
        (p["site_id"], p["url"]): p
        for p in json.loads((OUT / "propostas.json").read_text())["proposals"]
    }
    found = []
    for file in sorted((OUT / "testes").glob("*.json")):
        data = json.loads(file.read_text())
        for item in data["results"]:
            key = (data["source"]["id"], item["url"])
            if key in wanted:
                result = item["result"]
                found.append(
                    dict(
                        source_id=key[0],
                        url=item["url"],
                        kind=result["requested_kind"],
                        model=item["model"],
                        label="",
                        result=result,
                    )
                )
    return found


def main() -> int:
    """Executa ``vet`` ou ``apply``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["vet", "apply"])
    stage = parser.parse_args().stage
    vetted_file = OUT / "vetados.json"
    if stage == "vet":
        found = candidates()
        print(len(found), "candidatos", file=sys.stderr)
        vetted = vet_candidates(
            found,
            datetime.now(UTC),
            _real_reader,
            progress=lambda d, t: d % 50 == 0 and print(f"{d}/{t}", file=sys.stderr, flush=True),
        )
        vetted_file.write_text(json.dumps(vetted, ensure_ascii=False))
        print(Counter(v["proposal"] for v in vetted))
        return 0
    store = PostgresCatalogStore()
    vetted = json.loads(vetted_file.read_text())
    print(apply_mapping(store, vetted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
