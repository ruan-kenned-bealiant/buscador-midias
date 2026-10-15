"""Exportação de lotes de rotulagem às cegas.

O CSV que a pessoa preenche traz só o que ela precisa para julgar (fonte, URL, manchete e um trecho
da matéria): **não** mostra o veredito do modelo, o grupo, os temas nem as entidades, para não
enviesar o julgamento. A chave com tudo isso fica em ``chave.csv`` e só é usada na pontuação.
"""

from __future__ import annotations

import csv
import random
import re
from dataclasses import dataclass
from pathlib import Path

from buscador_midias.state.store import LABEL_GROUPS, LabelCandidate, StateStore

EXCERPT_CHARS = 600
NO_TEXT = "(texto não baixado: só a manchete)"
BLIND_COLUMNS = (
    "id",
    "fonte",
    "url",
    "manchete",
    "trecho",
    "veredito",
    "entidades_nomeadas",
    "obs",
)
KEY_COLUMNS = ("id", "lote", "url_hash", "grupo", "temas", "entidades", "fato", "evidencia")
KEY_FILE = "chave.csv"
_BATCH_FILE = re.compile(r"^rotulagem-(\d+)\.csv$")


@dataclass(frozen=True, slots=True)
class Batch:
    """Lote exportado.

    Attributes:
        number: Número do lote (``rotulagem-NN.csv``).
        path: Arquivo que a pessoa preenche.
        counts: Quantas URLs de cada grupo entraram.
    """

    number: int
    path: Path
    counts: dict[str, int]


def export_batch(store: StateStore, folder: Path, *, per_group: int, seed: int) -> Batch:
    """Sorteia um lote novo (sem repetir URLs já exportadas) e grava o CSV cego e a chave.

    Os itens dos três grupos são embaralhados, então a ordem do CSV não revela o grupo. O mesmo
    ``seed`` sobre o mesmo estado gera o mesmo lote.

    Args:
        store: Estado com triagem, análises e evidências.
        folder: Pasta de saída (criada se preciso).
        per_group: Itens por grupo (um grupo menor exporta o que tiver).
        seed: Semente do sorteio.

    Returns:
        O lote exportado.
    """
    folder.mkdir(parents=True, exist_ok=True)
    key_path = folder / KEY_FILE
    used_hashes, last_id = _existing_key(key_path)
    number = (
        max(
            (
                int(m.group(1))
                for p in folder.glob("rotulagem-*.csv")
                if (m := _BATCH_FILE.match(p.name))
            ),
            default=0,
        )
        + 1
    )

    rng = random.Random(seed)
    chosen: list[LabelCandidate] = []
    counts: dict[str, int] = {}
    for group in LABEL_GROUPS:
        pool = [c for c in store.label_pool(group) if c.url_hash not in used_hashes]
        picked = rng.sample(pool, min(per_group, len(pool)))
        counts[group] = len(picked)
        chosen.extend(picked)
    rng.shuffle(chosen)

    blind_path = folder / f"rotulagem-{number:02d}.csv"
    new_key = not key_path.exists()
    with (
        blind_path.open("w", encoding="utf-8-sig", newline="") as blind_file,
        key_path.open("a", encoding="utf-8-sig" if new_key else "utf-8", newline="") as key_file,
    ):
        blind = csv.writer(blind_file)
        key = csv.writer(key_file)
        blind.writerow(BLIND_COLUMNS)
        if new_key:
            key.writerow(KEY_COLUMNS)
        for offset, item in enumerate(chosen, start=1):
            label_id = f"L{last_id + offset:04d}"
            blind.writerow(
                [label_id, item.site_key, item.url, item.headline or "", _excerpt(item), "", "", ""]
            )
            key.writerow(
                [
                    label_id,
                    number,
                    item.url_hash,
                    item.group,
                    ",".join(item.themes),
                    "; ".join(item.entity_names),
                    item.adverse_fact,
                    item.evidence_provider or "",
                ]
            )
    return Batch(number=number, path=blind_path, counts=counts)


def _excerpt(item: LabelCandidate) -> str:
    """Início do texto da matéria, em uma linha, ou o aviso de que só há a manchete."""
    if not item.excerpt:
        return NO_TEXT
    flat = " ".join(item.excerpt.split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[:EXCERPT_CHARS].rstrip() + "…"


def _existing_key(path: Path) -> tuple[set[str], int]:
    """Hashes já exportados e o último número de id usado (0 se a chave não existe)."""
    if not path.is_file():
        return set(), 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    last = max((int(row["id"][1:]) for row in rows), default=0)
    return {row["url_hash"] for row in rows}, last
