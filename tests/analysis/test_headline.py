import pytest

from buscador_midias.analysis.headline import headline_text, tokens


def test_tokens_sem_acento_minusculos_e_sem_numeros():
    assert tokens("Operação da PF prende empresário por R$ 655 mil") == [
        "operacao",
        "da",
        "pf",
        "prende",
        "empresario",
        "por",
        "mil",
    ]


def test_tokens_ignora_letra_solta_e_id_colado_em_digitos():
    assert tokens("tse mantem condenacao 3327847e 127007h") == ["tse", "mantem", "condenacao"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Vorcaro depõe na PF - Gazeta do Povo", ["vorcaro", "depoe", "na", "pf"]),
        ("Vorcaro depõe na PF | Gazeta do Povo", ["vorcaro", "depoe", "na", "pf"]),
        (
            "Lula - presidente reage forte ao caso",
            ["lula", "presidente", "reage", "forte", "ao", "caso"],
        ),
        ("Guarani - Sport", ["guarani", "sport"]),  # nada sobra se tirar o sufixo: mantém tudo
    ],
)
def test_sufixo_de_veiculo_so_sai_quando_e_curto_e_sobra_manchete(title, expected):
    assert tokens(title) == expected


def test_manchete_prefere_o_titulo_do_news_sitemap():
    text = headline_text("https://a.com/x/algo-qualquer-123", "  PF prende  empresário ")

    assert text == "PF prende empresário"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://g1.globo.com/politica/noticia/2026/10/15/tse-mantem-condenacao-de-ex-deputado.ghtml",
            "tse mantem condenacao de ex deputado",
        ),
        (
            "https://www.folhadelondrina.com.br/esporte/diretor-denuncia-lobby-507474.html",
            "diretor denuncia lobby 507474",
        ),
        (
            "https://tribune.com.pk/story/2635052/irans-midday-meal-takes-top-prize",
            "irans midday meal takes top prize",
        ),
        (
            "https://www.mediapart.fr/journal/france/131026/fusillade-dans-un-college",
            "fusillade dans un college",
        ),
        ("https://a.com/a%C3%A7%C3%A3o/materia-com-acento", "materia com acento"),
    ],
)
def test_sem_titulo_usa_o_slug_da_url(url, expected):
    assert headline_text(url, None) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://tribune.com.pk/multimedia/videos/2255890",
        "https://tribune.com.pk/",
        "https://tribune.com.pk/entertainment",
        "",
    ],
)
def test_url_sem_slug_nao_tem_manchete(url):
    assert headline_text(url, None) == ""
