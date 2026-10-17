from dataclasses import replace
from datetime import timedelta

from tests.strategy.helpers import HOME, NOW, make_site

from buscador_midias.domain.mapping import MapPage, MapSource, PageStatus
from buscador_midias.strategy.mapping_priorities import HEADERS, priority_row

ARTICLE = HOME + "/politica/materia-com-titulo-do-jornal"


def row(pages, checks):
    return dict(zip(HEADERS, priority_row(make_site(), pages, checks, now=NOW), strict=True))


def test_prioriza_fonte_lida_sem_mecanismo_recente_e_distingue_feed_sem_data():
    page = MapPage(HOME, "", "root", PageStatus.OK, checked_at=NOW, article_urls=(ARTICLE,))
    source = MapSource(HOME + "/feed", "undated", "feed", (ARTICLE,), NOW)
    result = row([page], [source])
    assert result["Prioridade"] == 1
    assert result["Mecanismos sem data no escopo"] == 1
    assert result["Mecanismos recentes no escopo"] == 0


def test_mecanismo_recente_com_lacuna_tem_prioridade_sobre_amostras_correspondentes():
    page = MapPage(HOME, "", "root", PageStatus.OK, checked_at=NOW, article_urls=(ARTICLE,))
    source = MapSource(HOME + "/feed", "fresh", "feed", (ARTICLE + "-outro",), NOW)
    assert row([page], [source])["Prioridade"] == 2
    source = replace(source, listed_urls=(ARTICLE,))
    assert row([page], [source])["Prioridade"] == 4


def test_feed_antigo_e_fora_do_escopo_nao_comprova_mecanismo_recente():
    page = MapPage(HOME, "", "root", PageStatus.OK, checked_at=NOW)
    source = MapSource(HOME + "/feed", "fresh", "feed", (ARTICLE,), NOW - timedelta(days=2))
    outside = replace(source, listed_urls=("https://outro.com/noticia",), checked_at=NOW)
    assert row([page], [source, outside])["Prioridade"] == 1


def test_acesso_bloqueado_fica_para_revisao_e_nao_dispara_retentativa():
    page = MapPage(HOME, "", "root", PageStatus.BLOCKED, detail="robots.txt: HTTP 403")
    result = row([page], [])
    assert result["Prioridade"] == 3
    assert result["Situação"] == "acesso bloqueado"
