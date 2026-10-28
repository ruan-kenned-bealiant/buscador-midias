"""Sonda de um link do cadastro: lê o documento ao vivo e propõe o que fazer, sem alterar nada.

Proposta (``proposal``): ``keep``, ``inactivate`` (mais de 1 mês sem notícia), ``delete`` (1 ano
ou mais), ``remove_theme`` (tema inútil), ``remove_historical`` (período do endereço já
encerrado) ou ``review`` (a sonda não consegue decidir sozinha). Todo ``review`` traz ``cause``:
``access``, ``unreadable``, ``undated`` ou ``sports``. Falha de acesso nunca remove.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from time import sleep as wait_seconds
from typing import Any, Protocol

from buscador_midias.catalog.freshness import document_freshness, link_verdict
from buscador_midias.catalog.link_period import is_historical, url_period
from buscador_midias.catalog.link_theme import REMOVE, link_theme
from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.reader import TestLimitError
from buscador_midias.catalog.store import CatalogError, CatalogStore
from buscador_midias.net.fetcher import FetchResult

PROPOSALS = {"inactivate": "inactivate", "delete": "delete", "keep": "keep"}
# Falha que costuma passar sozinha: repetir com espera antes de mandar o link para revisão.
TRANSIENT_STATUS = (429, 500, 502, 503, 504)
TRANSIENT_ERRORS = (
    "host_cooldown",
    "DNS excedeu",
    "timed out",
    "Remote end closed",
    "Connection reset",
)
MAX_WAIT = 60
# Quando o transporte do link é recusado, estes são tentados antes de desistir. Perfis de robô
# declarado (``curl_bot_*``) ficam de fora: identificam-se como robô, que é justo o que bloqueia.
ALTERNATE_TRANSPORTS = ("curl_chrome_h1", "curl_chrome_h2", "curl_firefox_h2", "direct")
TRANSPORT_STATUS = (401, 402, 403, 406)
TRANSPORT_ERRORS = ("redirect_cycle", "SSL", "Remote end closed", "Connection reset")
# Sites sondados ao mesmo tempo: mantém sites x links por vez abaixo dos 32 trabalhadores de DNS.
MAX_SITES = 12


class Reader(Protocol):
    """Qualquer leitor com ``get`` que devolva ``FetchResult`` sem levantar erro de rede."""

    def get(self, url: str) -> FetchResult:
        """Lê a URL e devolve o resultado, inclusive em falha."""
        ...


def _transient(status: int | None, error: str | None) -> bool:
    if status in TRANSIENT_STATUS:
        return True
    return status is None and any(token in (error or "") for token in TRANSIENT_ERRORS)


def _outcome(link: dict[str, Any], proposal: str, reason: str = "", **found: Any) -> dict[str, Any]:
    return dict(
        transport=link.get("access_transport", "direct"),
        link_id=link["id"],
        link_version=link["version"],
        source_id=link["source_id"],
        url=link["url"],
        label=link.get("label", ""),
        kind=link["kind"],
        model=link["model"],
        proposal=proposal,
        reason=reason,
        **found,
    )


def decide_by_address(link: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """Remoção que o próprio endereço já prova (tema inútil, período encerrado): sem baixar nada."""
    theme = link_theme(link["url"], link.get("label", ""))
    if theme and theme["action"] == REMOVE:
        reason = f"tema inútil: {theme['theme']}"
        return _outcome(link, "remove_theme", reason, theme=theme["theme"])
    if is_historical(link["url"], link["model"], now):
        period = url_period(link["url"])
        reason = f"período encerrado: {period.start} a {period.end}"  # type: ignore[union-attr]
        return _outcome(link, "remove_historical", reason)
    return None


def review_link(link: dict[str, Any], reader: Reader, now: datetime) -> dict[str, Any]:
    """Decide a proposta de um link pelo endereço e pela data mais nova do documento."""
    if decided := decide_by_address(link, now):
        return decided
    theme = link_theme(link["url"], link.get("label", ""))
    try:
        response = reader.get(link["url"])
    except TestLimitError as error:
        return _outcome(link, "review", f"acesso: {error}", cause="access", transient=True)
    if response.status != 200 or not response.content:
        why = response.error or f"HTTP {response.status}"
        return _outcome(
            link,
            "review",
            f"acesso: {why}",
            cause="access",
            status=response.status,
            transient=_transient(response.status, response.error),
            retry_after=response.retry_after_seconds,
        )
    try:
        document = parse_document(response.content, link["url"])
    except Exception:  # noqa: BLE001 - qualquer corpo ilegível vira revisão, nunca remoção
        return _outcome(
            link,
            "review",
            "o documento não é feed nem sitemap legível",
            cause="unreadable",
            status=response.status,
        )
    found = document_freshness(document, now)
    verdict = link_verdict(found, now)
    detail = dict(
        status=response.status,
        entries=len(document["entries"]) or len(document["children"]),
        newest=found.newest.isoformat() if found.newest else None,
        age_days=(now - found.newest).days if found.newest else None,
        basis=found.basis,
        exact=found.exact,
        theme=theme["theme"] if theme else None,
    )
    if verdict == "unknown":
        return _outcome(
            link, "review", "sem data de publicação no documento", cause="undated", **detail
        )
    if theme and verdict == "keep":
        return _outcome(
            link, "review", "esporte: confirmar se o feed traz escândalo", cause="sports", **detail
        )
    return _outcome(link, PROPOSALS[verdict], **detail)


def review_with_retry(
    link: dict[str, Any],
    make_reader: Callable[[dict[str, Any]], Reader],
    now: datetime,
    *,
    attempts: int = 3,
    sleep: Callable[[float], None] = wait_seconds,
) -> dict[str, Any]:
    """Repete só a falha transitória, esperando o que o site pediu (no máximo ``MAX_WAIT`` s).

    Cada tentativa usa um leitor novo, com orçamento próprio. Recusa (403, 402, 406…) não repete.
    """
    for attempt in range(1, attempts + 1):
        reader = None if decide_by_address(link, now) else make_reader(link)
        found = review_link(link, reader, now)  # type: ignore[arg-type]
        if not found.get("transient") or attempt == attempts:
            return found
        sleep(min(found.get("retry_after") or 2 * attempt, MAX_WAIT))
    return found


def transport_fixable(outcome: dict[str, Any]) -> bool:
    """Recusa que outro transporte costuma resolver: ciclo, 401/402/403/406, falha de TLS."""
    if outcome.get("cause") != "access":
        return False
    reason = outcome.get("reason", "")
    return outcome.get("status") in TRANSPORT_STATUS or any(m in reason for m in TRANSPORT_ERRORS)


def review_with_transports(
    link: dict[str, Any],
    make_reader: Callable[[dict[str, Any]], Reader],
    now: datetime,
    *,
    sleep: Callable[[float], None] = wait_seconds,
) -> dict[str, Any]:
    """Sonda o link e, se o transporte dele foi recusado, tenta os outros uma vez cada.

    Só uma recusa que outro transporte costuma resolver dispara a busca (404, 410, timeout e 5xx
    não). Um transporte só conta se chega a um documento legível; página de captcha não conta.
    O resultado traz ``transport`` (o que funcionou) e ``transport_changed``.
    """
    first = review_with_retry(link, make_reader, now, sleep=sleep)
    if not transport_fixable(first):
        return first
    current = link.get("access_transport", "direct")
    for transport in ALTERNATE_TRANSPORTS:
        if transport == current:
            continue
        found = review_with_retry(
            {**link, "access_transport": transport}, make_reader, now, attempts=1, sleep=sleep
        )
        if found.get("cause") not in ("access", "unreadable"):
            return {**found, "transport_changed": True}
    return first


def load_links(store: CatalogStore, site_ids: Iterable[int]) -> list[dict[str, Any]]:
    """Links ativos e visíveis dos sites pedidos, com a versão que a sonda deve respeitar."""
    ids = list(site_ids)
    marks = ",".join("?" * len(ids))
    with store.connection() as db:
        rows = db.execute(
            "SELECT l.id,l.version,l.source_id,l.url,l.label,l.kind,l.model,l.access_transport "
            "FROM links l "
            "JOIN sites s ON s.id=l.source_id WHERE l.source_id IN (" + marks + ") "
            "AND l.deleted_at IS NULL AND l.active=1 AND s.deleted_at IS NULL "
            "ORDER BY l.source_id,l.id",
            ids,
        ).fetchall()
    return [dict(row) for row in rows]


def review_batch(
    links: Iterable[dict[str, Any]],
    now: datetime,
    make_reader: Callable[[dict[str, Any]], Reader],
    *,
    per_host: int = 3,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Sonda os links dados: sites em paralelo, ``per_host`` links por vez em cada um.

    O resultado segue a ordem de entrada dentro de cada site; links ainda sem ``id`` (candidatos)
    também são aceitos, pois a sonda só lê a URL.
    """
    by_site: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        by_site[link["source_id"]].append(link)
    total = sum(len(group) for group in by_site.values())
    done = 0

    def one(link: dict[str, Any]) -> dict[str, Any]:
        nonlocal done
        found = review_with_retry(link, make_reader, now)
        done += 1
        if progress:
            progress(done, total)
        return found

    def site(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with ThreadPoolExecutor(max_workers=per_host) as pool:
            return list(pool.map(one, group))

    if not by_site:
        return []
    with ThreadPoolExecutor(max_workers=min(len(by_site), MAX_SITES)) as sites:
        batches = list(sites.map(site, by_site.values()))
    return [item for batch in batches for item in batch]


def review_sites(
    store: CatalogStore,
    site_ids: Iterable[int],
    now: datetime,
    make_reader: Callable[[dict[str, Any]], Reader],
    *,
    per_host: int = 3,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Sonda todos os links ativos dos sites pedidos, ordenados pelo id do link."""
    found = review_batch(
        load_links(store, site_ids), now, make_reader, per_host=per_host, progress=progress
    )
    return sorted(found, key=lambda i: i["link_id"])


def apply_proposals(store: CatalogStore, results: Iterable[dict[str, Any]]) -> Counter[str]:
    """Aplica inativação e exclusão pelo cadastro, na versão sondada; link editado é pulado.

    ``review`` e ``keep`` não mudam nada. A exclusão é lógica (``deleted_at``), como a da tela.
    """
    done: Counter[str] = Counter()
    for item in results:
        proposal = item["proposal"]
        try:
            if proposal == "inactivate":
                store.save(
                    "endpoint", dict(version=item["link_version"], active=False), item["link_id"]
                )
            elif proposal in ("delete", "remove_theme", "remove_historical"):
                store.delete("endpoint", item["link_id"], item["link_version"])
        except CatalogError as error:
            if error.status != 409:
                raise
            done["conflict"] += 1
            continue
        done[proposal] += 1
    return done
