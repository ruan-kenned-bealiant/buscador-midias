"""Treina e avalia o filtro L1 (manchete) com o corpus de Mídias IA e as URLs já vistas.

Uso (precisa das variáveis ``DB_MIDIAS_IA_*`` no ambiente; o banco abre somente leitura):
    python scripts/train_l1.py [--state var/state.db] [--out var/l1_model.json] [--recall 0.98]

Exemplos positivos: títulos do corpus em português (``origin`` importado ou busca ao vivo), **sem**
os artigos de contexto "estrito" (tema confirmado e alerta), que ficam de fora do treino para servir
de teste limpo. Exemplos negativos: manchetes (título do news sitemap ou slug) das URLs vistas nos
sites do idioma coberto, depois do L0, excluindo o que o corpus já tinha.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from buscador_midias.sites.loader import load_sites
from buscador_midias.triage.headline import headline_text
from buscador_midias.triage.l0 import l0_filter
from buscador_midias.triage.l1 import DEFAULT_LANGUAGES, train_l1

ROOT = Path(__file__).resolve().parents[1]
_POSITIVES_SQL = """
SELECT a.titulo, COALESCE(bool_or(c.tema_confirmado AND c.alerta), false) AS strict
FROM midias_ia_articles a
LEFT JOIN midias_ia_article_contexts c ON c.article_id = a.id
WHERE a.titulo IS NOT NULL AND a.titulo <> '' AND lower(COALESCE(a.language, '')) LIKE 'pt%'
GROUP BY a.id
"""


def corpus_titles() -> tuple[list[str], list[str]]:
    """Títulos do corpus em português: (positivos gerais, positivos estritos), somente leitura."""
    import psycopg

    connection = psycopg.connect(
        host=os.environ["DB_MIDIAS_IA_HOST"],
        port=os.environ["DB_MIDIAS_IA_PORT"],
        dbname=os.environ["DB_MIDIAS_IA_DATABASE"],
        user=os.environ["DB_MIDIAS_IA_USERNAME"],
        password=os.environ["DB_MIDIAS_IA_PASSWORD"],
        options="-c default_transaction_read_only=on",
    )
    general: list[str] = []
    strict: list[str] = []
    with connection, connection.cursor() as cursor:
        cursor.execute(_POSITIVES_SQL)
        for title, is_strict in cursor:
            (strict if is_strict else general).append(title)
    return general, strict


def seen_headlines(state: Path, languages: frozenset[str]) -> list[str]:
    """Manchetes das URLs vistas nos sites dos idiomas cobertos (após L0, sem as do corpus)."""
    site_language = {site.key: site.language for site in load_sites(ROOT / "data/sites.csv").sites}
    connection = sqlite3.connect(state)
    headlines: list[str] = []
    for site_key, url, title in connection.execute(
        "SELECT site_key, url, title FROM urls_seen WHERE kind != 'known'"
    ):
        if site_language.get(site_key) not in languages or l0_filter(url) is not None:
            continue
        text = headline_text(url, title)
        if text:
            headlines.append(text)
    return headlines


def main(argv: list[str]) -> int:
    """Treina, imprime as métricas e grava o filtro."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=ROOT / "var/state.db")
    parser.add_argument("--out", type=Path, default=ROOT / "var/l1_model.json")
    parser.add_argument("--recall", type=float, default=0.98)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    general, strict = corpus_titles()
    negatives = seen_headlines(args.state, DEFAULT_LANGUAGES)
    print(
        f"positivos gerais {len(general)} | estritos (só teste) {len(strict)} | "
        f"negativos {len(negatives)}"
    )

    chosen, evaluation = train_l1(general, negatives, recall=args.recall, seed=args.seed)
    strict_recall = sum(chosen.keeps(title) for title in strict) / max(len(strict), 1)
    print(
        f"limiar {evaluation.threshold:+.2f} | recall alvo {evaluation.recall_target:.2f} | "
        f"recall (teste geral, n={evaluation.test_positives}) {evaluation.positive_recall:.3f} | "
        f"recall ESTRITO (n={len(strict)}) {strict_recall:.3f} | "
        f"negativos que passam (n={evaluation.test_negatives}) {evaluation.negative_pass_rate:.3f}"
    )
    chosen.save(args.out)
    print(f"filtro gravado em {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
