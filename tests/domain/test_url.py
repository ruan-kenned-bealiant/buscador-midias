"""Paridade com o MidiasIaUrlCanonicalizer do KyX.

O golden file (tests/fixtures/canonical_golden.json) é gerado rodando o código PHP real, via
scripts/generate_canonical_golden.php. O corpus existente foi hasheado por essa regra: qualquer
divergência faria o crawler tratar como novo o que o corpus já tem.
"""

import json
from pathlib import Path

import pytest

from buscador_midias.domain.url import canonicalize_url, url_domain, url_hash

GOLDEN = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "canonical_golden.json").read_text(
        encoding="utf-8"
    )
)["cases"]


@pytest.mark.parametrize("case", GOLDEN, ids=[c["url"].strip() or "vazio" for c in GOLDEN])
def test_paridade_com_o_php_real(case: dict[str, str | None]):
    url = str(case["url"])

    assert canonicalize_url(url) == case["canonical"]
    assert url_hash(url) == case["hash"]
    assert url_domain(url) == case["domain"]


def test_golden_cobre_os_casos_relevantes():
    assert len(GOLDEN) >= 50


# --- casos legíveis (espelham MidiasIaUrlCanonicalizerTest do KyX) ---


def test_forca_https_independente_do_esquema_original():
    assert canonicalize_url("http://noticia.com/artigo") == "https://noticia.com/artigo"


def test_remove_www_e_baixa_o_host_mas_nao_o_caminho():
    url = "https://WWW.Noticia.com/Artigo-Com-Maiuscula"

    assert canonicalize_url(url) == "https://noticia.com/Artigo-Com-Maiuscula"


def test_remove_www_numerado():
    assert canonicalize_url("https://www2.noticia.com/artigo") == "https://noticia.com/artigo"


def test_caminho_vazio_vira_barra():
    assert canonicalize_url("https://noticia.com") == "https://noticia.com/"


def test_remove_barra_final_fora_da_raiz():
    assert canonicalize_url("https://noticia.com/artigo/") == "https://noticia.com/artigo"


def test_remove_qualquer_porta():
    assert canonicalize_url("https://noticia.com:8080/artigo") == "https://noticia.com/artigo"


def test_remove_ponto_final_do_host():
    assert canonicalize_url("https://noticia.com./artigo") == "https://noticia.com/artigo"


def test_descarta_fragmento():
    assert canonicalize_url("https://noticia.com/artigo#secao") == "https://noticia.com/artigo"


def test_remove_parametros_de_rastreio_e_ordena_o_resto():
    url = "https://noticia.com/artigo?utm_source=newsletter&page=2&ref=home&id=5"

    assert canonicalize_url(url) == "https://noticia.com/artigo?id=5&page=2"


def test_nao_remove_msclkid():
    url = "https://noticia.com/artigo?msclkid=abc123"

    assert canonicalize_url(url) == url


def test_urls_equivalentes_geram_o_mesmo_hash_de_64_caracteres():
    a = url_hash("http://WWW.Noticia.com/artigo/?utm_source=x&ref=y")
    b = url_hash("https://noticia.com/artigo?utm_source=x&ref=y")

    assert a == b
    assert len(a) == 64


def test_dominio_sem_www_em_minusculas():
    assert url_domain("https://WWW.Noticia.com/artigo") == "noticia.com"


# --- armadilhas de paridade documentadas ---


def test_espaco_na_query_vira_mais_e_til_vira_percent_7e():
    assert canonicalize_url("https://n.com/a?q=a%20b&r=~") == "https://n.com/a?q=a+b&r=%7E"


def test_chaves_numericas_ordenam_como_numero_chaves_de_texto_byte_a_byte():
    assert canonicalize_url("https://n.com/a?10=b&9=a") == "https://n.com/a?9=a&10=b"
    assert canonicalize_url("https://n.com/a?b=1&A=2") == "https://n.com/a?A=2&b=1"


def test_so_o_ascii_do_host_e_baixado():
    assert canonicalize_url("https://ÉPOCA.com/x") == "https://Época.com/x"


def test_canonicalize_faz_trim_mas_domain_nao():
    assert canonicalize_url("  https://noticia.com/a  ") == "https://noticia.com/a"
    assert url_domain("  https://noticia.com/a  ") is None


def test_url_inparseavel_volta_como_veio_sem_espacos():
    assert canonicalize_url(" http:///x ") == "http:///x"
