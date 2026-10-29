"""Sonda de um link do cadastro: lê o documento ao vivo e propõe o que fazer, sem alterar nada.

Proposta (``proposal``): ``keep``, ``inactivate`` (mais de 1 mês sem notícia), ``delete`` (1 ano
ou mais), ``remove_theme`` (tema inútil), ``remove_historical`` (período do endereço já
encerrado) ou ``review`` (a sonda não consegue decidir sozinha). Todo ``review`` traz ``cause``:
``access``, ``unreadable``, ``undated`` ou ``sports``. Falha de acesso nunca remove.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from time import sleep as wait_seconds
from typing import Any, Protocol

from buscador_midias.catalog.freshness import document_freshness, link_verdict
from buscador_midias.catalog.http_transport import make_reader as transport_reader
from buscador_midias.catalog.link_period import is_historical, url_period
from buscador_midias.catalog.link_theme import REMOVE, link_theme
from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.reader import TestLimitError
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
    detail: dict[str, Any] = dict(
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


def real_reader(link: dict[str, Any]) -> Reader:
    """Leitor com o transporte que o cadastro já descobriu ser necessário para o link."""
    reader: Reader = transport_reader(
        [link["url"]],
        lambda step: None,
        transport=link.get("access_transport", "direct"),
        seconds=25,
        requests=4,
    )
    return reader
