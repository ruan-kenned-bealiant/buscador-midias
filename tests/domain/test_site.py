import pytest

from buscador_midias.domain.site import Site


def make_site(url: str) -> Site:
    return Site.create(
        name="Fonte",
        url=url,
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


def test_host_remove_www():
    site = make_site("https://www.folha.uol.com.br")

    assert site.host == "folha.uol.com.br"


def test_host_preserva_www_numerado():
    assert make_site("https://www12.senado.leg.br/noticias").host == "www12.senado.leg.br"
    assert make_site("https://www3.nhk.or.jp").host == "www3.nhk.or.jp"


def test_url_preserva_www_e_esquema_para_o_fetch():
    assert make_site("http://www.cubadebate.cu").url == "http://www.cubadebate.cu"


def test_url_descarta_barra_final_query_e_fragmento():
    site = make_site("https://en.yna.co.kr/?a=1#x")

    assert site.url == "https://en.yna.co.kr"


def test_path_prefix_vazio_para_dominio_inteiro():
    assert make_site("https://oglobo.globo.com/").path_prefix == ""


def test_path_prefix_sem_barra_final():
    site = make_site("https://g1.globo.com/globonews/")

    assert site.path_prefix == "/globonews"
    assert site.url == "https://g1.globo.com/globonews"


def test_key_combina_host_e_prefixo():
    assert make_site("https://www.bbc.com/portuguese").key == "bbc.com/portuguese"
    assert make_site("https://www.bbc.com").key == "bbc.com"


@pytest.mark.parametrize("url", ["", "folha.uol.com.br", "ftp://x.com", "https://"])
def test_url_invalida_levanta_value_error(url: str):
    with pytest.raises(ValueError):
        make_site(url)


@pytest.mark.parametrize(
    "candidate",
    [
        "https://folha.uol.com.br/mundo/noticia.shtml",
        "https://www.folha.uol.com.br/mundo/noticia.shtml",
        "http://folha.uol.com.br/",
    ],
)
def test_owns_aceita_mesmo_host_com_ou_sem_www(candidate: str):
    assert make_site("https://www.folha.uol.com.br").owns(candidate)


def test_owns_aceita_subdominios_do_host_da_fonte():
    site = make_site("https://folha.uol.com.br")

    assert site.owns("https://www1.folha.uol.com.br/mundo/a.shtml")
    assert site.owns("https://m.folha.uol.com.br/a")


def test_owns_rejeita_outro_host_irmao_e_sufixo_sem_ponto():
    site = make_site("https://g1.globo.com")

    assert not site.owns("https://outro.com.br/a")
    assert not site.owns("https://oglobo.globo.com/a")
    assert not site.owns("https://globo.com/a")
    assert not site.owns("https://meug1.globo.com/a")


def test_owns_subdominio_respeita_prefixo_de_secao():
    site = make_site("https://folha.uol.com.br/mundo")

    assert site.owns("https://www1.folha.uol.com.br/mundo/a.shtml")
    assert not site.owns("https://www1.folha.uol.com.br/poder/a.shtml")


def test_owns_respeita_prefixo_de_secao():
    site = make_site("https://g1.globo.com/globonews/")

    assert site.owns("https://g1.globo.com/globonews")
    assert site.owns("https://g1.globo.com/globonews/noticia/x.ghtml")
    assert not site.owns("https://g1.globo.com/politica/x.ghtml")


def test_owns_nao_confunde_prefixo_parcial():
    site = make_site("https://g1.globo.com/globonews")

    assert not site.owns("https://g1.globo.com/globonews-extra/x")


def test_owns_rejeita_esquema_nao_http():
    assert not make_site("https://folha.uol.com.br").owns("ftp://folha.uol.com.br/a")
