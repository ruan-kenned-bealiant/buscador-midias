from dataclasses import replace

from buscador_midias.domain.mapping import MapPage, PageStatus
from buscador_midias.domain.site import Site
from buscador_midias.strategy.structural_report import (
    EXAMINED,
    PARTIAL,
    RESTRICTED,
    UNCONFIRMED,
    summarize_structure,
)

SITE = Site.create(name="Fonte", url="https://a.com", region="", country="",
                   country_code="", language="", source_type="")
ROOT = MapPage("https://a.com", "", "root", PageStatus.OK)
SECTION = MapPage("https://a.com/politica", ROOT.url, "section", PageStatus.OK,
                  page_type="section", type_basis="schema", discovery_basis="navigation")


def test_entrada_sozinha_nao_comprova_estrutura():
    assert summarize_structure(SITE, [ROOT]).outcome == UNCONFIRMED


def test_estrutura_examinada_exige_ausencia_de_pendencias_e_falhas():
    assert summarize_structure(SITE, [ROOT, SECTION]).outcome == EXAMINED
    for status in (PageStatus.PENDING, PageStatus.ERROR, PageStatus.DISALLOWED):
        extra = replace(SECTION, url="https://a.com/economia", status=status)
        assert summarize_structure(SITE, [ROOT, SECTION, extra]).outcome == PARTIAL


def test_restricao_na_entrada_preserva_secoes_ja_mapeadas():
    root = replace(ROOT, detail="robots: HTTP 403; origem a.com; fila preservada")
    summary = summarize_structure(SITE, [root, SECTION])
    assert summary.outcome == RESTRICTED
    assert summary.sections_confirmed == 1


def test_paginas_de_servico_taxonomia_e_materias_nao_sao_secoes_pendentes():
    extra = [replace(SECTION, url=f"https://a.com/{kind}", page_type=kind,
                     status=PageStatus.EXCLUDED) for kind in ("article", "service", "taxonomy")]
    summary = summarize_structure(SITE, [ROOT, SECTION, *extra])
    assert summary.outcome == EXAMINED
    assert (summary.pending, summary.articles, summary.services, summary.taxonomies) == (0, 1, 1, 1)


def test_candidata_lida_na_navegacao_nao_vira_secao_confirmada():
    summary = summarize_structure(SITE, [ROOT, replace(SECTION, page_type="unknown")])
    assert summary.outcome == EXAMINED
    assert summary.sections_confirmed == 0
    assert summary.sections_read_unknown == 1


def test_caminhos_inferidos_lidos_nao_confirmam_sozinhos_a_estrutura():
    candidate = replace(SECTION, page_type="unknown", discovery_basis="mechanism_path")
    assert summarize_structure(SITE, [ROOT, candidate]).outcome == UNCONFIRMED


def test_falha_ao_ler_politica_nao_e_rotulada_como_proibicao_explicita():
    root = replace(ROOT, status=PageStatus.PENDING,
                   detail="robots.txt: ConnectTimeout; fila preservada")
    assert summarize_structure(SITE, [root, SECTION]).outcome == UNCONFIRMED
