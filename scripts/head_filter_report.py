"""Resume o corte pelo topo (``head_filter``) de uma rodada: o que mediu e se já dá para ativar.

Lê as métricas das tentativas de descoberta. Com o corte ativo, ``head_audit_sampled`` conta os
documentos da amostra de auditoria (lidos por inteiro): neles, ``head_shadow_would_skip`` é o que
o corte teria dispensado e ``head_shadow_missed`` o que ele teria perdido; a perda estimada é
``missed / would_skip``. Em modo sombra, ``head_shadow_would_skip`` conta
documentos que o corte teria dispensado e ``head_shadow_missed`` os que, mesmo assim, trouxeram
URLs da janela. A regra só deve ser ativada com ``missed == 0``. Sem argumento usa a rodada mais
recente; ``--run-id`` escolhe outra.
"""

from __future__ import annotations

import argparse
import json

from buscador_midias.catalog.postgres import PostgresCatalogStore

KEYS = (
    "head_audit_sampled",
    "head_shadow_would_skip",
    "head_shadow_missed",
    "head_cut",
    "head_stopped_download",
    "read_order_contradicted",
)


def main() -> int:
    """Imprime a soma de cada métrica e o veredito."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    store = PostgresCatalogStore()
    with store.connection() as db:
        latest = db.execute(
            "SELECT id FROM ingest_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        run = args.run_id or (latest["id"] if latest else None)
        sums = ",".join(f"COALESCE(sum((a.metrics->>'{k}')::int),0) {k}" for k in KEYS)
        row = db.execute(
            f"SELECT count(*) documents,{sums} FROM ingest_attempts a "  # noqa: S608 - chaves fixas
            "JOIN ingest_jobs j ON j.id=a.job_id WHERE j.stage='discovery' AND j.run_id=%s",
            (run,),
        ).fetchone()
    report = dict(run=run, **dict(row))
    verdict = (
        "sem corte medido nesta rodada"
        if not report["head_shadow_would_skip"] and not report["head_cut"]
        else "pode ativar (enforce): nenhum corte perdeu URL"
        if not report["head_shadow_missed"]
        else "NÃO ativar: houve corte que teria perdido URL da janela"
    )
    if report["head_audit_sampled"]:
        verdict = (
            f"auditoria: {report['head_audit_sampled']} documentos lidos por inteiro, "
            f"{report['head_shadow_would_skip']} o corte teria dispensado, "
            f"{report['head_shadow_missed']} teriam perdido URL da janela"
        )
    print(json.dumps(dict(report, verdict=verdict), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
