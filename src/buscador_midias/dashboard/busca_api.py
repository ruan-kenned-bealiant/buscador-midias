"""API local da busca sobre as tabelas ``bm_*``, sob o mesmo token e proteção de origem."""

from __future__ import annotations

import re
from typing import Any

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.panel import broker_status
from buscador_midias.db.serialize import jsonable

_HASH = re.compile(r"/busca/noticias/([a-f0-9]{64})")


def _int(query: dict[str, str], name: str, default: int = 0) -> int:
    try:
        return int(query.get(name, default))
    except ValueError:
        raise CatalogError("Parâmetro numérico inválido.") from None


def _snapshot() -> list[dict[str, Any]]:
    from buscador_midias.pipeline import broker

    return broker.snapshot()


def dispatch(
    api: Any, handler: Any, path: str, query: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    """Consulta e opera a busca; não altera o cadastro de sites e links."""
    reports = api.reports
    if handler.command == "GET":
        return 200, jsonable(_read(api, path, query))
    api._authorize(handler)
    data = api._body(handler)
    if handler.command == "PATCH" and path == "/busca/configuracao":
        return 200, jsonable(api.settings.configure_search(data))
    if handler.command == "POST" and path == "/busca/rodadas":
        return 202, jsonable(api.work.create_run(data))
    if handler.command == "POST" and path == "/busca/pendencias/reprocessar":
        return 202, reports.reopen(data)
    retry = re.fullmatch(r"/busca/noticias/([a-f0-9]{64})/reprocessar", path)
    if handler.command == "POST" and retry and not data:
        return 202, reports.reopen(dict(hashes=[retry[1]]))
    raise CatalogError("Endereço ou método não permitido.", 404)


def _read(api: Any, path: str, query: dict[str, str]) -> Any:
    reports = api.reports
    run_id = query.get("run_id", "")
    corpus = re.fullmatch(r"/busca/fontes/([1-9][0-9]*)/corpus", path)
    if corpus:
        return reports.corpus_articles(int(corpus[1]), run_id, _int(query, "offset"))
    if path == "/busca/fontes":
        return reports.source_progress(run_id, _int(query, "offset"), query.get("search", ""))
    if path == "/busca":
        result = reports.overview(
            offset=_int(query, "offset"),
            stage=query.get("stage", ""),
            state=query.get("state", ""),
            run_id=run_id,
            source_id=_int(query, "source_id"),
        )
        return dict(result, broker=broker_status(api.broker or _snapshot))
    if path == "/busca/painel":
        return reports.panel(_int(query, "days", 7), broker=api.broker or _snapshot)
    if path == "/busca/custos":
        return dict(metrics=reports.metrics(run_id=run_id, source_id=_int(query, "source_id")))
    if path == "/busca/pendencias":
        return reports.pending(
            offset=_int(query, "offset"),
            stage=query.get("stage", ""),
            reason=query.get("reason", ""),
            reason_class=query.get("reason_class", ""),
            run_id=run_id,
            source_id=_int(query, "source_id"),
        )
    article = _HASH.fullmatch(path)
    if article:
        return reports.article(article[1])
    raise CatalogError("Endereço não encontrado.", 404)
