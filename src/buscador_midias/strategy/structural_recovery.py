"""Saneamento conservador de candidatas estruturais refutadas por leitura anterior."""

from dataclasses import replace

from buscador_midias.domain.mapping import MapPage, PageStatus
from buscador_midias.domain.site import Site
from buscador_midias.strategy.mapping import normalize_page


def retire_refuted_candidate(site: Site, page: MapPage) -> MapPage | None:
    """Descarta somente palpites sem evidência de seção ou navegação declarada.

    Uma resposta 404 num link do menu continua sendo uma lacuna. Entradas, seções já
    confirmadas e falhas transitórias também permanecem para revisão/leitura. Preserva
    URL, origem, tentativas, datas e amostras; o detalhe guarda a resposta que refutou o palpite.
    """
    if (
        page.kind == "root"
        or page.discovery_basis not in ("url_shape", "mechanism_path")
        or page.page_type != "unknown"
        or page.status not in (PageStatus.ERROR, PageStatus.INVALID)
    ):
        return None
    reason = ""
    if page.detail in ("HTTP 404", "HTTP 410"):
        reason = "caminho inferido inexistente, sem evidência de seção ou navegação"
    elif page.status is PageStatus.INVALID and page.detail == "resposta não é HTML":
        reason = "recurso não HTML inferido como seção"
    elif page.status is PageStatus.INVALID and page.detail.startswith(
        "redirecionamento para fora da seção/fonte: "
    ):
        destination = page.detail.partition(": ")[2]
        if normalize_page(destination) == normalize_page(site.url):
            reason = "caminho inferido redireciona para a entrada, sem seção própria confirmada"
    if not reason:
        return None
    return replace(
        page,
        status=PageStatus.EXCLUDED,
        detail=f"candidata refutada: {reason}; evidência anterior: {page.detail}",
    )
