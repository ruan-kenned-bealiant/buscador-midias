import pytest

from buscador_midias.discovery.url_filter import DEFAULT_OFF_TOPIC_SEGMENTS, FilterReason, l0_filter


@pytest.mark.parametrize(
    "url",
    [
        "https://tribune.com.pk/story/2635052/irans-midday-meal-takes-top-prize",
        "https://www.yeniduzen.com/sorusturma-bilerek-ilerletilmiyor-164750h.htm",
        "https://tribune.com.pk/anti-coup",
        "https://g1.globo.com/politica/noticia/2026/10/15/governo-anuncia-medida.ghtml",
        "https://www.hani.co.kr/arti/politics/politics_general/1234.html",
        "https://exemplo.com/2026/10/15/titulo-da-materia",
        "https://exemplo.com/economia/negocios/empresa-e-alvo-de-investigacao",
        "https://exemplo.com/a%C3%A7%C3%A3o/materia-com-acento",
        # "category" e "page" são parte do caminho da matéria em alguns sites, não listagem
        "https://www.freemalaysiatoday.com/category/world/2026/10/17/israeli-settlers-attack-us",
        "https://www.globaltimes.cn/page/202610/1372421.shtml",
    ],
)
def test_materias_passam(url):
    assert l0_filter(url) is None


@pytest.mark.parametrize(
    "url", ["https://tribune.com.pk/", "https://tribune.com.pk", "https://a.com/?x=1"]
)
def test_pagina_inicial(url):
    assert l0_filter(url) is FilterReason.HOME


@pytest.mark.parametrize(
    "url",
    [
        "https://www.storm.mg/keyword/123456",
        "https://www.38north.org/tag/cars/",
        "https://exemplo.com/tags/politica",
        "https://exemplo.com/author/joao-silva",
        "https://exemplo.com/autor/joao-silva",
        "https://exemplo.com/category/noticias/",
        "https://exemplo.com/topics/economia",
        "https://exemplo.com/pt-br/tag/eleicoes",
        "https://exemplo.com/TAG/Eleicoes",
        "https://exemplo.com/politica/page/2/",
        "https://exemplo.com/search/corrupcao",
        "https://exemplo.com/category/noticias/page/3/",
        "https://exemplo.com/tag/economia/2",
        "https://www.theelephant.info/product/donate-one-time/",
        "https://exemplo.com/shop/camiseta",
    ],
)
def test_paginas_de_listagem(url):
    assert l0_filter(url) is FilterReason.LISTING


@pytest.mark.parametrize(
    "url",
    [
        "https://tribune.com.pk/entertainment",
        "https://www.postguam.com/obituaries/",
        "https://exame.com/newsletters/",
        "https://www.channel4.com/news",
    ],
)
def test_secao_de_uma_palavra_nao_e_materia(url):
    assert l0_filter(url) is FilterReason.SECTION


@pytest.mark.parametrize(
    "url",
    [
        "https://exemplo.com/2026",  # tem dígito: não arriscamos
        "https://exemplo.com/foto.jpg",
        "https://exemplo.com/a/b.png",
    ],
)
def test_so_uma_palavra_sem_digito_ponto_ou_hifen_e_secao(url):
    assert l0_filter(url) is not FilterReason.SECTION


@pytest.mark.parametrize(
    "url",
    [
        "https://exemplo.com/img/foto.jpg",
        "https://exemplo.com/img/FOTO.PNG",
        "https://exemplo.com/video/clip.mp4",
        "https://exemplo.com/sitemap/outro.xml",
    ],
)
def test_arquivo_que_nao_e_pagina_de_materia(url):
    assert l0_filter(url) is FilterReason.ASSET


def test_pdf_nao_e_descartado():
    assert l0_filter("https://exemplo.com/docs/nota-oficial.pdf") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://exemplo.com/esporte/futebol/botafogo-vence-o-classico",
        "https://exemplo.com/Esportes/rodada-do-brasileirao",
        "https://exemplo.com/receitas/bolo-de-cenoura-facil",
        "https://exemplo.com/horoscopo/signos-do-dia",
    ],
)
def test_secoes_fora_do_tema(url):
    assert l0_filter(url, off_topic_segments=DEFAULT_OFF_TOPIC_SEGMENTS) is FilterReason.OFF_TOPIC


def test_por_padrao_nao_descarta_por_tema():
    assert l0_filter("https://exemplo.com/esporte/futebol/cartola-denuncia-desvio") is None


def test_tema_so_vale_como_segmento_de_caminho_nao_como_parte_do_slug():
    topics = DEFAULT_OFF_TOPIC_SEGMENTS
    assert (
        l0_filter(
            "https://exemplo.com/politica/governo-corta-verba-do-esporte", off_topic_segments=topics
        )
        is None
    )
    assert l0_filter("https://exemplo.com/politica/esporte", off_topic_segments=topics) is None


def test_lista_de_temas_fora_e_configuravel():
    url = "https://exemplo.com/esporte/futebol/botafogo-vence-o-classico"

    assert l0_filter(url) is None
    assert l0_filter(url, off_topic_segments=frozenset({"futebol"})) is FilterReason.OFF_TOPIC


def test_url_malformada_nunca_levanta():
    assert l0_filter("") is None
    assert l0_filter("http://[::1") is None
