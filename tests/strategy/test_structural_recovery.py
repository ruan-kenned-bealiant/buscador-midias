from dataclasses import replace

from tests.strategy.helpers import NOW, make_site

from buscador_midias.domain.mapping import MapPage, PageStatus
from buscador_midias.strategy.structural_recovery import retire_refuted_candidate

SITE = make_site()
PAGE = MapPage(SITE.url + "/economia", SITE.url, "section", PageStatus.ERROR,
               checked_at=NOW, attempts=2, detail="HTTP 404", discovery_basis="mechanism_path")


def test_descarta_palpite_refutado_preservando_evidencias():
    result = retire_refuted_candidate(SITE, PAGE)
    assert result.status is PageStatus.EXCLUDED
    assert result.url == PAGE.url and result.found_on == PAGE.found_on
    assert result.checked_at == NOW and result.attempts == 2
    assert "HTTP 404" in result.detail


def test_nao_apaga_lacuna_de_navegacao_entrada_ou_secao_confirmada():
    for changed in (replace(PAGE, discovery_basis="navigation"),
                    replace(PAGE, kind="root"), replace(PAGE, page_type="section")):
        assert retire_refuted_candidate(SITE, changed) is None


def test_nao_descarta_falhas_transitorias_nem_candidatas_pendentes():
    for changed in (replace(PAGE, detail="HTTP 503"),
                    replace(PAGE, detail="ConnectError: DNS"),
                    replace(PAGE, status=PageStatus.PENDING)):
        assert retire_refuted_candidate(SITE, changed) is None


def test_descarta_recurso_binario_e_retorno_a_entrada_mas_nao_destino_novo():
    invalid = replace(PAGE, status=PageStatus.INVALID, detail="resposta não é HTML")
    assert retire_refuted_candidate(SITE, invalid) is not None
    prefix = "redirecionamento para fora da seção/fonte: "
    assert retire_refuted_candidate(SITE, replace(invalid, detail=prefix + SITE.url)) is not None
    other = replace(invalid, detail=prefix + SITE.url + "/novo")
    assert retire_refuted_candidate(SITE, other) is None
