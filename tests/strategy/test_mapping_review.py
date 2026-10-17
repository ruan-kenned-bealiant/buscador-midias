import pytest
from tests.strategy.helpers import HOME, make_site

from buscador_midias.domain.mapping import MapPage, PageStatus
from buscador_midias.strategy.mapping_export import mapping_tables
from buscador_midias.strategy.mapping_review import review_entry


def test_falha_ao_ler_robots_nao_vira_proibicao_explicita():
    blocked = review_entry(MapPage(HOME, "", "root", detail="robots.txt: HTTP 403"))
    denied = review_entry(MapPage(HOME, "", "root", detail="robots.txt proíbe o acesso à raiz"))
    assert blocked is not None and denied is not None
    assert blocked.category == "robots: leitura bloqueada"
    assert denied.category == "robots: proibição explícita"
    assert not blocked.transient and not denied.transient


@pytest.mark.parametrize(
    ("detail", "category", "transient"),
    [
        ("robots.txt: HTTP 503", "robots: indisponível", True),
        ("robots.txt: HTTP 429", "limite de requisições", False),
        ("HTTP 429", "limite de requisições", False),
        ("ConnectError: [Errno -3] Temporary failure in name resolution", "DNS transitório", True),
        ("ConnectError: [Errno -2] Name or service not known", "DNS: domínio a revisar", False),
        ("ReadTimeout: The read operation timed out", "rede/HTTP transitório", True),
        ("ConnectError: [Errno 104] Connection reset by peer", "rede/HTTP transitório", True),
        ("HTTP 502", "rede/HTTP transitório", True),
        ("[SSL: CERTIFICATE_VERIFY_FAILED] Hostname mismatch", "TLS", False),
        ("HTTP 402", "HTTP a revisar", False),
        (
            "redirecionamento para fora da seção/fonte: https://novo.com",
            "redirecionamento fora do escopo",
            False,
        ),
    ],
)
def test_triagem_nao_sugere_retentar_falhas_permanentes_ou_limites(detail, category, transient):
    issue = review_entry(MapPage(HOME, "", "root", PageStatus.ERROR, detail=detail))
    assert issue is not None
    assert issue.category == category
    assert issue.transient is transient


def test_entrada_bem_sucedida_e_falha_em_subsecao_nao_entram_na_revisao_de_entradas():
    assert review_entry(MapPage(HOME, "", "root", PageStatus.OK)) is None
    assert (
        review_entry(
            MapPage(HOME + "/politica", HOME, "section", PageStatus.ERROR, detail="HTTP 502")
        )
        is None
    )


def test_relatorio_preserva_leituras_validas_apesar_de_falha_posterior_no_robots():
    page = MapPage(HOME, "", "root", PageStatus.OK, detail="robots.txt: HTTP 403")
    tables = mapping_tables([make_site()], {"exemplo.com": [page]}, {})
    row = dict(zip(tables["Revisão"][0], tables["Revisão"][1][0], strict=True))
    assert row["Estado"] == "ok"
    assert row["Páginas lidas na fonte"] == 1
    assert row["Motivo"] == page.detail
    assert row["Grupo de revisão"] == "robots: leitura bloqueada"
    assert row["Falha transitória"] == "não"
    assert page.status is PageStatus.OK
