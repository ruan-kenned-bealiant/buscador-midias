"""Filtro de conteúdo rotineiro: regras estreitas, com proteção contra perda de notícia adversa."""

import pytest

from buscador_midias.ingestion.content_filter import (
    Exclusion,
    category_exclusion,
    title_exclusion,
    url_exclusion,
)


@pytest.mark.parametrize(
    "url,category",
    [
        ("https://site.example/astrologia/horoscopo-libra-hoy-25-octubre", "horoscope"),
        ("https://site.example/horoskop/clanak/skorpionima-danas", "horoscope"),
        ("https://site.example/astrology/horoscope/daily-career-horoscope", "horoscope"),
        ("https://site.example/mg/previsao-do-tempo/noticia/2026/10/25/previsao", "weather"),
        ("https://site.example/mexico/2026/10/24/pronostico-del-clima-en-acapulco", "weather"),
        ("https://site.example/loterias/resultado-federal-concurso-6112/", "lottery"),
        ("https://site.example/food/recipes/pasta-al-limone", "cooking"),
    ],
)
def test_url_rules_catch_only_routine_sections(url, category):
    found = url_exclusion(url)
    assert (found.category if found else None) == category
    assert found is None or found.stage == "url"


@pytest.mark.parametrize(
    "url",
    [
        # Seções amplas escondem notícia adversa: nunca são excluídas só pela URL.
        "https://site.example/esportes/futebol/bernie-ecclestone-se-declara-inocente-apos-fraude",
        "https://site.example/entretenimento/globo-confirma-encontro-de-flavio",
        "https://site.example/noticia/mega-sena-fraude-investigada-pela-policia",
        "https://site.example/weather/tornado-kills-five-in-ohio",
        "https://site.example/economia/receitas/arrecadacao-federal-cresce",
        "https://site.example/noticia/receita-federal-combate-fraude-tributaria",
        "https://site.example/brasil/tse-desmente-ex-bbb-que-alegou-mudanca-de-local",
        "https://site.example/esporte/flamengo-protesta-contra-escalacao-de-rojo",
    ],
)
def test_url_rules_never_exclude_broad_sections_or_ambiguous_words(url):
    assert url_exclusion(url) is None


@pytest.mark.parametrize(
    "title,category",
    [
        ("Horóscopo do dia: previsões para os 12 signos nesta segunda-feira (26)", "horoscope"),
        ("Horoscope Today, October 25, 2026: Mindful detachment", "horoscope"),
        ("Previsão do tempo hoje para Salvador (BA): sol com muitas nuvens", "weather"),
        ("Mega-Sena sorteia prêmio de R$ 230 milhões neste sábado: dezenas e prêmio", "lottery"),
        ("Resultado da Quina 7144: ninguém acerta dezenas e prêmio acumula", "lottery"),
        ("Palmeiras x Corinthians pelo Brasileirão: onde assistir, horário e escalação", "match"),
        ("Palpites para Flamengo x Bahia: prováveis escalações", "match"),
        ("Receita de empadão de frango e requeijão", "cooking"),
        ("Como fazer pavê de chocolate com bolacha maizena?", "cooking"),
        ("Resumo da novela: capítulo de hoje de A Nobreza do Amor", "tv_recap"),
    ],
)
def test_title_rules_catch_routine_content(title, category):
    found = title_exclusion("https://site.example/materia", title)
    assert found and found.category == category and found.stage == "title"


@pytest.mark.parametrize(
    "title",
    [
        "Receita Federal combate fraude tributária de R$ 277 milhões",
        "Gilmar favorece Lula e suspende dívida de R$ 18 milhões com a Receita",
        "Receita de R$ 3 bilhões cresce no trimestre",
        "Universidade confirma que ex-BBB fraudou sistema de cotas",
        "Bernie Ecclestone se declara inocente após acusação de fraude",
        "Mega-Sena: polícia investiga fraude em apostas e prende gerente da lotérica",
        "Flamengo protesta contra escalação de Rojo e tenta eliminar o Estudiantes",
        "Frente fria deixa três mortos e desabrigados no Sul",
        "Horóscopo: astrólogo é preso por estelionato",
        "STJD julga o goleiro por doping e pode suspender o jogo",
    ],
)
def test_adverse_or_ambiguous_titles_are_protected(title):
    assert title_exclusion("https://site.example/materia", title) is None


def test_title_rule_also_uses_url_hints_but_keeps_the_risk_guard():
    url = "https://site.example/weather/storm-boelo-weather-warnings-ireland"
    assert title_exclusion(url, "Weather warnings for Ireland as storm approaches") is not None
    assert title_exclusion(url, "Storm kills two as weather warnings stay in force") is None
    assert title_exclusion(url, None) is None


def test_category_barrier_blocks_only_blocked_categories_without_adverse_finding():
    blocked = ["horoscope", "weather", "cooking", "sports"]
    found = category_exclusion("sports", False, blocked)
    assert found == Exclusion("sports", "ai_category", "ai")
    # Matéria esportiva com fato adverso (doping, fraude) continua no fluxo.
    assert category_exclusion("sports", True, blocked) is None
    assert category_exclusion("news", False, blocked) is None
    assert category_exclusion("opinion", False, blocked) is None
    assert category_exclusion("entertainment", False, blocked) is None
    assert category_exclusion(None, False, blocked) is None
    assert category_exclusion("sports", False, []) is None


@pytest.mark.parametrize(
    "url,title",
    [
        (
            "https://site.example/noticia/criminosos-matam-pessoas-em-rituais-mega-sena",
            "Criminosos matam pessoas em rituais para ganhar na Mega-Sena",
        ),
        (
            "https://site.example/esporte/flamengo-recorre-conmebol-escalacao-de-marcos-rojo",
            "Flamengo recorre à Conmebol contra escalação de Marcos Rojo pelo Estudiantes",
        ),
        (
            "https://site.example/noticias/colisao-na-supervia-mega-sena-haddad-e-guaido",
            "Notícias do dia: colisão na SuperVia, Mega-Sena, Haddad e Guaidó",
        ),
    ],
)
def test_url_hints_in_the_slug_do_not_override_adverse_titles(url, title):
    assert title_exclusion(url, title) is None
