"""Triagem das entradas do inventário, sem alterar escopo nem políticas de acesso."""

import re
from dataclasses import dataclass

from buscador_midias.domain.mapping import MapPage, PageStatus


@dataclass(frozen=True, slots=True)
class EntryReview:
    """Motivo e próximo passo sugerido; não dispara retentativas automaticamente."""

    category: str
    transient: bool
    action: str


def review_entry(page: MapPage) -> EntryReview | None:
    """Distingue proibição explícita de falha ao ler robots, preservando o motivo.

    Só trata entradas com falha ou observação. ``transient`` sugere uma nova tentativa
    limitada; não permite ignorar robots, TLS, bloqueios ou o intervalo entre requisições.
    Uma falha na entrada pode coexistir com leituras válidas de outras páginas da fonte.
    """
    if page.kind != "root" or (
        page.status
        not in (PageStatus.ERROR, PageStatus.BLOCKED, PageStatus.DISALLOWED, PageStatus.INVALID)
        and not page.detail
    ):
        return None
    detail = page.detail.lower()
    match = re.search(r"\bhttp\s+(\d{3})\b", detail)
    status = int(match[1]) if match else None
    if page.status is PageStatus.DISALLOWED or "robots.txt proíbe" in detail:
        return EntryReview(
            "robots: proibição explícita",
            False,
            "Manter a restrição; revisar apenas o escopo permitido pela política.",
        )
    if "redirecionamento para fora" in detail:
        return EntryReview(
            "redirecionamento fora do escopo",
            False,
            "Conferir destino e identidade editorial antes de corrigir o catálogo.",
        )
    if status == 429:
        return EntryReview(
            "limite de requisições",
            False,
            "Aguardar e revisar o intervalo antes de uma nova tentativa limitada.",
        )
    if "robots" in detail:
        return EntryReview(
            "robots: leitura bloqueada" if status in (401, 403) else "robots: indisponível",
            status in (500, 502, 503, 504, 520),
            "Revisar a resposta do robots; a política não foi lida e as páginas seguem suspensas.",
        )
    if "temporary failure in name resolution" in detail:
        return EntryReview(
            "DNS transitório",
            True,
            "Repetir uma tentativa limitada após normalização do DNS.",
        )
    if any(word in detail for word in ("name or service not known", "getaddrinfo")):
        return EntryReview(
            "DNS: domínio a revisar",
            False,
            "Conferir domínio e resolução DNS antes de nova leitura.",
        )
    if any(word in detail for word in ("certificate", "ssl", "tls")):
        return EntryReview(
            "TLS",
            False,
            "Revisar domínio e certificado; manter a validação TLS.",
        )
    if any(word in detail for word in ("timeout", "timed out", "connection reset")) or status in (
        500,
        502,
        503,
        504,
        520,
    ):
        return EntryReview(
            "rede/HTTP transitório",
            True,
            "Repetir uma tentativa limitada, preservando o intervalo.",
        )
    if page.status is PageStatus.BLOCKED or "challenge" in detail:
        return EntryReview(
            "acesso bloqueado",
            False,
            "Revisar acesso HTTP gratuito e registrar a restrição.",
        )
    if page.page_type in ("article", "service"):
        return EntryReview(
            "entrada sem seção",
            False,
            "Conferir se a URL cadastrada representa a seção editorial.",
        )
    if status is not None:
        return EntryReview(
            "HTTP a revisar",
            False,
            "Conferir a resposta HTTP antes de nova tentativa.",
        )
    return EntryReview(
        "conteúdo inválido" if page.status is PageStatus.INVALID else "leitura não concluída",
        False,
        "Revisar o motivo registrado antes de retomar a entrada.",
    )
