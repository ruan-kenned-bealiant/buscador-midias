"""URLs com data embutida (um sitemap por dia ou por mês): data, modelo e renderização."""

from datetime import UTC, datetime

import pytest

from buscador_midias.domain.dated_url import find_date, render, render_window, templatize

NOW = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("url", "template"),
    [
        (
            "https://x.com/arc/sitemap/2026-10-06/?outputType=xml",
            "https://x.com/arc/sitemap/{YYYY}-{MM}-{DD}/?outputType=xml",
        ),
        (
            "https://g1.globo.com/sitemap/g1/2026/10/07_1.xml",
            "https://g1.globo.com/sitemap/g1/{YYYY}/{MM}/{DD}_1.xml",
        ),
        ("https://x.com/sitemap-20261007.xml", "https://x.com/sitemap-{YYYY}{MM}{DD}.xml"),
        ("https://x.com/sitemap-news-2026-10.xml", "https://x.com/sitemap-news-{YYYY}-{MM}.xml"),
        ("https://x.com/sitemap-202610.xml", "https://x.com/sitemap-{YYYY}{MM}.xml"),
        (
            "https://x.com/sitemap.xml?yyyy=2026&d=2026-10-07",
            "https://x.com/sitemap.xml?yyyy=2026&d={YYYY}-{MM}-{DD}",
        ),
    ],
)
def test_url_com_data_recente_vira_modelo(url: str, template: str):
    assert templatize(url, NOW) == template


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/post-sitemap-2019-04.xml",  # arquivo de outro ano
        "https://x.com/2019/05/30/materia.xml",
        "https://x.com/sitemap.xml",
        "https://2026-10-07.x.com/sitemap.xml",  # a data no host não conta
        "https://x.com/sitemap/2026-13-45.xml",  # data impossível
        "https://x.com/materia-1791397794",  # número comprido
    ],
)
def test_url_sem_data_recente_fica_como_esta(url: str):
    assert templatize(url, NOW) == url


def test_dia_de_ate_45_dias_atras_ainda_vira_modelo_e_mais_antigo_nao():
    assert "{DD}" in templatize("https://x.com/s/2026-09-10.xml", NOW)
    assert "{DD}" not in templatize("https://x.com/s/2026-08-01.xml", NOW)


def test_mes_corrente_e_os_dois_anteriores_viram_modelo_e_mais_antigo_nao():
    assert "{MM}" in templatize("https://x.com/s-2026-08.xml", NOW)
    assert "{MM}" not in templatize("https://x.com/s-2026-06.xml", NOW)


def test_renderiza_o_modelo_com_a_data_de_hoje():
    template = "https://x.com/arc/{YYYY}-{MM}-{DD}/?outputType=xml"

    assert render(template, NOW) == "https://x.com/arc/2026-10-16/?outputType=xml"


def test_url_sem_modelo_nao_muda_ao_renderizar():
    assert render("https://x.com/sitemap.xml", NOW) == "https://x.com/sitemap.xml"


def test_janela_de_hoje_e_ontem_cobre_a_virada_do_dia_em_outro_fuso():
    window = render_window("https://x.com/s/{YYYY}/{MM}/{DD}.xml", NOW)

    assert window == ("https://x.com/s/2026/10/16.xml", "https://x.com/s/2026/10/15.xml")


def test_modelo_mensal_tem_uma_so_url_na_janela_e_na_virada_do_mes_tem_duas():
    template = "https://x.com/s-{YYYY}-{MM}.xml"

    assert render_window(template, NOW) == ("https://x.com/s-2026-10.xml",)
    first = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    assert render_window(template, first) == (
        "https://x.com/s-2026-11.xml",
        "https://x.com/s-2026-10.xml",
    )


def test_url_sem_modelo_tem_janela_de_uma_url():
    assert render_window("https://x.com/sitemap.xml", NOW) == ("https://x.com/sitemap.xml",)


def test_find_date_le_o_dia_ou_o_mes_da_url_para_ordenar_filhos():
    assert find_date("https://x.com/sitemap/2026-10-15/?o=xml") == datetime(
        2026, 10, 15, tzinfo=UTC
    )
    assert find_date("https://x.com/s-2026-10.xml") == datetime(2026, 10, 1, tzinfo=UTC)
    assert find_date("https://x.com/sitemap/latest/") is None
    assert find_date("https://x.com/sitemap/1999-07-22/") == datetime(1999, 7, 22, tzinfo=UTC)
