"""Aplicação das propostas de mapeamento ao cadastro, depois de revalidar cada candidato.

O teste do mapeamento prova que o link entrega notícia com texto, mas não olha a data: uma
rota de arquivo parada há anos passaria. Por isso cada candidato passa de novo pela mesma
sonda da revisão do catálogo (tema útil e notícia no último mês) e só ``keep`` vira link
ativo, criado pelo próprio cadastro com a prova que o testou.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from buscador_midias.catalog.link_review import Reader, review_batch
from buscador_midias.catalog.store import CatalogError, CatalogStore


def vet_candidates(
    candidates: Iterable[dict[str, Any]],
    now: datetime,
    make_reader: Callable[[dict[str, Any]], Reader],
    *,
    per_host: int = 2,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Sonda os candidatos e devolve cada proposta junto da prova (``result``) que o testou.

    Candidato: ``source_id``, ``url``, ``kind``, ``model``, ``result`` e, se houver, ``label``
    e ``access_transport`` (o do teste; é com ele que a sonda lê).
    """
    items = [
        dict(
            id=None,
            version=None,
            access_transport=c["result"].get("access_transport", "direct"),
            **{k: v for k, v in c.items() if k != "result"},
        )
        for c in candidates
    ]
    proofs = {(c["source_id"], c["url"]): c["result"] for c in candidates}
    found = review_batch(items, now, make_reader, per_host=per_host, progress=progress)
    return [{**item, "result": proofs[(item["source_id"], item["url"])]} for item in found]


def apply_mapping(store: CatalogStore, vetted: Iterable[dict[str, Any]]) -> Counter[str]:
    """Cria como ativo só o que a sonda manteve; o que o cadastro já conhece nunca é recriado.

    Contagens: ``created``, ``skipped`` (sonda não manteve), ``exists`` (link já cadastrado,
    ativo, inativo ou excluído: a decisão anterior vale) e ``refused`` (prova de outra
    configuração ou mecanismo alterado).
    """
    done: Counter[str] = Counter()
    for item in vetted:
        if item["proposal"] != "keep":
            done["skipped"] += 1
            continue
        data = dict(
            url=item["url"],
            kind=item["kind"],
            model=item["model"],
            active=True,
            access_transport=item["result"].get("access_transport", "direct"),
        )
        try:
            store.save("endpoint", data, source_id=item["source_id"], test_result=item["result"])
        except CatalogError as error:
            done["exists" if "já está cadastrado" in str(error) else "refused"] += 1
            continue
        done["created"] += 1
    return done
