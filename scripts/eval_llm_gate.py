"""Mede o triador por LLM: recall nos artigos "estritos" do corpus e descarte nas manchetes reais.

Uso (precisa de ``OPENROUTER_API_KEY`` e ``DB_MIDIAS_IA_*`` no ambiente; o corpus abre somente
leitura; faz chamadas pagas ao OpenRouter):
    python scripts/eval_llm_gate.py [--model M] [--positives 300] [--per-site 20] [--out CSV]

Positivos: títulos de artigos do corpus com tema confirmado e alerta (devem ser mantidos).
Negativos: manchetes das URLs vistas pela descoberta (depois do L0), até ``--per-site`` por fonte.
O CSV de saída traz cada decisão com o motivo, para auditoria humana.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import httpx

from buscador_midias.sites.loader import load_sites
from buscador_midias.triage.headline import headline_text
from buscador_midias.triage.l0 import l0_filter
from buscador_midias.triage.openrouter import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    OpenRouterClassifier,
)
from buscador_midias.triage.prompt import Policy, prompt_version, system_prompt
from buscador_midias.triage.relevance import Decision, HeadlineItem, classify_all

ROOT = Path(__file__).resolve().parents[1]
_STRICT_SQL = """
SELECT a.titulo, COALESCE(a.source_domain, '')
FROM midias_ia_articles a
JOIN midias_ia_article_contexts c ON c.article_id = a.id
WHERE a.titulo IS NOT NULL AND a.titulo <> '' AND c.tema_confirmado AND c.alerta
GROUP BY a.id
"""


def strict_positives(limit: int, rng: random.Random) -> list[tuple[str, str]]:
    """Amostra de (título, domínio) de artigos estritos do corpus, somente leitura."""
    import psycopg

    connection = psycopg.connect(
        host=os.environ["DB_MIDIAS_IA_HOST"],
        port=os.environ["DB_MIDIAS_IA_PORT"],
        dbname=os.environ["DB_MIDIAS_IA_DATABASE"],
        user=os.environ["DB_MIDIAS_IA_USERNAME"],
        password=os.environ["DB_MIDIAS_IA_PASSWORD"],
        options="-c default_transaction_read_only=on",
    )
    with connection, connection.cursor() as cursor:
        cursor.execute(_STRICT_SQL)
        rows = [(title, domain) for title, domain in cursor]
    return rng.sample(rows, min(limit, len(rows)))


def real_headlines(state: Path, per_site: int, rng: random.Random) -> list[tuple[str, str, str]]:
    """Amostra de (manchete, fonte, idioma) das URLs vistas após o L0, ``per_site`` por fonte."""
    language = {s.key: s.language for s in load_sites(ROOT / "data/sites.csv").sites}
    by_site: dict[str, list[str]] = defaultdict(list)
    for site, url, title in sqlite3.connect(state).execute(
        "SELECT site_key, url, title FROM urls_seen WHERE kind != 'known'"
    ):
        text = headline_text(url, title)
        if text and l0_filter(url) is None:
            by_site[site].append(text)
    sample: list[tuple[str, str, str]] = []
    for site, texts in sorted(by_site.items()):
        for text in rng.sample(texts, min(per_site, len(texts))):
            sample.append((text, site, language.get(site, "?")))
    return sample


def main(argv: list[str]) -> int:
    """Roda a avaliação, imprime o resumo e grava o CSV de auditoria."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--policy", choices=[p.value for p in Policy], default=Policy.BALANCED.value
    )
    parser.add_argument("--positives", type=int, default=300)
    parser.add_argument("--per-site", type=int, default=20)
    parser.add_argument("--state", type=Path, default=ROOT / "var/state.db")
    parser.add_argument("--out", type=Path, default=ROOT / "var/llm_gate_eval.csv")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    policy = Policy(args.policy)
    rng = random.Random(args.seed)
    positives = strict_positives(args.positives, rng)
    negatives = real_headlines(args.state, args.per_site, rng)
    items: list[HeadlineItem] = []
    meta: list[tuple[str, str, str, str]] = []  # grupo, fonte, idioma, texto
    for title, domain in positives:
        items.append(HeadlineItem(len(items) + 1, domain or "corpus", title))
        meta.append(("positivo_estrito", domain, "Português", title))
    for text, site, lang in negatives:
        items.append(HeadlineItem(len(items) + 1, site, text))
        meta.append(("manchete_real", site, lang, text))
    print(
        f"prompt {prompt_version(policy)} | modelo {args.model} | "
        f"{len(positives)} positivos, {len(negatives)} reais"
    )

    with httpx.Client() as client:
        classifier = OpenRouterClassifier(
            client,
            api_key=os.environ["OPENROUTER_API_KEY"],
            model=args.model,
            system_prompt=system_prompt(policy),
            base_url=os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL),
        )
        verdicts = classify_all(classifier, items, batch_size=25, workers=4)
    usage = classifier.usage

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["grupo", "fonte", "idioma", "decisao", "temas", "motivo", "manchete"])
        for (group, site, lang, text), verdict in zip(meta, verdicts, strict=True):
            writer.writerow(
                [
                    group,
                    site,
                    lang,
                    verdict.decision.value,
                    ",".join(verdict.themes),
                    verdict.reason,
                    text,
                ]
            )

    pos = [v for (g, *_), v in zip(meta, verdicts, strict=True) if g == "positivo_estrito"]
    kept = sum(v.decision is not Decision.DISCARD for v in pos)
    print(f"RECALL nos estritos: {kept}/{len(pos)} = {kept / max(len(pos), 1):.3f}")
    by_lang: dict[str, Counter[str]] = defaultdict(Counter)
    for (group, _site, lang, _text), verdict in zip(meta, verdicts, strict=True):
        if group == "manchete_real":
            by_lang[lang][verdict.decision.value] += 1
    total: Counter[str] = Counter()
    for lang, counts in sorted(by_lang.items(), key=lambda item: -sum(item[1].values())):
        total.update(counts)
        n = sum(counts.values())
        print(
            f"  {lang:12s} n={n:4d} descartadas {counts['discard'] / n:5.1%} | "
            f"desconhecidas {counts['unknown']}"
        )
    n = sum(total.values())
    print(
        f"TOTAL reais n={n} descartadas {total['discard'] / n:.1%} | "
        f"desconhecidas {total['unknown']}"
    )
    cost = usage.cost_usd
    per_1000 = (cost / len(items) * 1000) if cost is not None else None
    print(
        f"uso: {usage.calls} chamadas, {usage.prompt_tokens} tokens entrada, "
        f"{usage.completion_tokens} saída, custo ${cost if cost is not None else '?'} "
        f"(~${per_1000 if per_1000 is not None else '?'} por 1000 manchetes), "
        f"{usage.retries} retries, {usage.errors} lotes com erro"
    )
    print(f"auditoria em {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
