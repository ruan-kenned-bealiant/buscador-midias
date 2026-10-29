"""Tela Artigos: os registros do corpus com entidades, contexto e alerta, por site ou busca livre."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from report_support import ARTICLE_CORPUS_DDL, seed

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.articles import ArticleRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


ZONE = ZoneInfo("America/Sao_Paulo")


def noon(days: int) -> datetime:
    """Meio-dia, em Brasília, ``days`` dias a partir de hoje (datas estáveis para os filtros)."""
    day = datetime.now(UTC).astimezone(ZONE).date() + timedelta(days=days)
    return datetime(day.year, day.month, day.day, 12, tzinfo=ZONE)


@pytest.fixture
def world(schema_name: str) -> dict[str, Any]:
    data = seed(schema_name)
    articles = ArticleRepository(schema_name, corpus_schema=schema_name)
    with articles.connection() as db:
        db.execute(ARTICLE_CORPUS_DDL)
        db.execute(
            "INSERT INTO midias_ia_articles(id,url_hash,url,titulo,domain,source_domain,source_name,"
            "origin,first_seen_by,published_at,created_at,evidencia_status,evidencia_path,"
            "ai_verdict,ai_score) VALUES "
            "(1,%(h1)s,'https://alfa.example/obra-atrasada','Obra atrasada em Brasília',"
            "'alfa.example','alfa.example','Alfa','crawler','buscador-midias',"
            "%(p1)s,%(c1)s,'captured','midias-ia/evidencias/a.html',"
            "'relevante',0.9),"
            "(2,%(h2)s,'https://beta.example/festival','Festival de música',"
            "'beta.example','beta.example','Beta','crawler','buscador-midias',"
            "%(c2)s,%(c2)s,'pending',NULL,'irrelevante',0.1),"
            "(3,%(h3)s,'https://alfa.example/antigo','Notícia importada',"
            "'alfa.example','alfa.example','Alfa','imported_corpus','ccnews',"
            "%(c3)s,%(c3)s,'captured','midias-ia/evidencias/c.html',"
            "NULL,NULL)",
            dict(
                h1="a" * 64,
                h2="b" * 64,
                h3="c" * 64,
                p1=noon(0) - timedelta(hours=3),
                c1=noon(0),
                c2=noon(-2),
                c3=noon(-9),
            ),
        )
        db.execute(
            "INSERT INTO midias_ia_article_entities(article_id,name,name_norm,entity_type,role,"
            "mention_count,context) VALUES "
            "(1,'João Silva','joao silva','PER','investigado',3,'Foi apontado por desvio de verba.'),"
            "(1,'Construtora Alfa','construtora alfa','ORG','outro',1,NULL),"
            "(2,'Maria Souza','maria souza','PER','outro',2,NULL),"
            "(3,'Pedro Lima','pedro lima','PER','outro',1,NULL)"
        )
        db.execute(
            "INSERT INTO midias_ia_article_contexts(article_id,entity_name,theme,alerta,confianca,"
            "justificativa,papel_entidade,fato_adverso,ai_verdict) VALUES "
            "(1,'João Silva','corrupção',true,0.95,'Citado como investigado em desvio.',"
            "'investigado','Desvio de verba na obra.','alerta'),"
            "(1,'Construtora Alfa',NULL,false,0.8,'Apenas contratada.','outro','','irrelevante'),"
            "(1,'Entidade Solta',NULL,false,0.5,'Sem linha de entidade.','outro','','irrelevante'),"
            "(2,'Maria Souza',NULL,false,0.9,'Cantora do festival.','outro','','irrelevante')"
        )
    return dict(articles=articles, **data)


def ids(result: dict[str, Any]) -> list[int]:
    return [item["id"] for item in result["items"]]


def test_the_newest_entries_come_first_with_everything_the_screen_needs(world):
    result = world["articles"].search(scope="all")

    assert ids(result) == [1, 2, 3]
    first = result["items"][0]
    assert first["url"] == "https://alfa.example/obra-atrasada"
    assert first["title"] == "Obra atrasada em Brasília" and first["source"] == "Alfa"
    assert first["entered_at"] > first["published_at"]
    assert first["evidence_url"] == "/evidencias/corpus/" + "a" * 64
    assert result["items"][1]["evidence_url"] is None
    assert first["alert_count"] == 1 and result["items"][1]["alert_count"] == 0


def test_each_entity_comes_with_its_context_and_alert(world):
    [article] = world["articles"].search(q="obra", field="title")["items"]

    by_name = {e["name"]: e for e in article["entities"]}
    joao = by_name["João Silva"]
    assert (joao["type"], joao["role"], joao["mentions"]) == ("PER", "investigado", 3)
    assert joao["context"] == "Foi apontado por desvio de verba."
    [analysis] = joao["analyses"]
    assert analysis["alert"] is True and analysis["theme"] == "corrupção"
    assert analysis["justification"] == "Citado como investigado em desvio."
    assert analysis["adverse_fact"] == "Desvio de verba na obra." and analysis["confidence"] == 0.95
    assert by_name["Construtora Alfa"]["analyses"][0]["alert"] is False
    # Análise sem linha de entidade não se perde.
    assert by_name["Entidade Solta"]["type"] is None
    assert by_name["Entidade Solta"]["analyses"][0]["justification"] == "Sem linha de entidade."
    assert joao["alert"] is True and by_name["Construtora Alfa"]["alert"] is False


def test_a_site_shows_only_its_own_articles(world):
    alfa, beta = world["sites"]

    assert ids(world["articles"].search(site_id=alfa["id"], scope="all")) == [1, 3]
    assert ids(world["articles"].search(site_id=beta["id"], scope="all")) == [2]
    with pytest.raises(CatalogError) as missing:
        world["articles"].search(site_id=99999)
    assert missing.value.status == 404


def test_the_default_scope_is_what_the_searcher_brought_in(world):
    assert ids(world["articles"].search()) == [1, 2]
    assert ids(world["articles"].search(scope="all")) == [1, 2, 3]


@pytest.mark.parametrize(
    ("query", "field", "expected"),
    [
        ("joao", "entity", [1]),  # sem diferenciar acento nem caixa
        ("JOÃO silva", "entity", [1]),
        ("construtora", "entity", [1]),
        ("festival", "url", [2]),
        ("música", "title", [2]),
        ("desvio", "alert", [1]),  # justificativa ou fato adverso da análise
        ("verba", "alert", [1]),
        ("cantora", "alert", [2]),
        ("joao", "all", [1]),
        ("beta.example", "all", [2]),
        ("festival", "all", [2]),
        ("verba", "all", []),  # "tudo" não varre o texto da análise: seria lento no corpus inteiro
        ("nada disso existe", "all", []),
    ],
)
def test_free_search_looks_in_the_chosen_field(world, query, field, expected):
    assert ids(world["articles"].search(q=query, field=field, scope="all")) == expected


def test_search_wildcards_are_taken_literally(world):
    assert ids(world["articles"].search(q="50%", field="title", scope="all")) == []
    assert ids(world["articles"].search(q="_e", field="url", scope="all")) == []


def test_the_alert_filter_keeps_only_articles_with_or_without_alert(world):
    assert ids(world["articles"].search(alert="yes", scope="all")) == [1]
    assert ids(world["articles"].search(alert="no", scope="all")) == [2, 3]


def test_the_entry_period_limits_the_result(world):
    articles = world["articles"]

    assert ids(articles.search(since=_day(-1), scope="all")) == [1]
    assert ids(articles.search(until=_day(-5), scope="all")) == [3]
    assert ids(articles.search(since=_day(-3), until=_day(-1), scope="all")) == [2]
    assert ids(articles.search(since=_day(-2), until=_day(-2), scope="all")) == [2]


def test_pagination_reports_the_total_and_the_page(world):
    page = world["articles"].search(scope="all", limit=2)

    assert (page["total"], page["offset"], len(page["items"]), page["capped"]) == (3, 0, 2, False)
    assert ids(world["articles"].search(scope="all", limit=2, offset=2)) == [3]


def test_invalid_requests_are_refused_with_a_clear_message(world):
    articles = world["articles"]
    for kwargs in (
        dict(field="outro"),
        dict(alert="talvez"),
        dict(scope="x"),
        dict(q="a"),
        dict(q="x" * 101),
        dict(since="30/10/2026"),
        dict(until="ontem"),
        dict(offset=-1),
        dict(offset=1_000_001),
        dict(site_id=-3),
    ):
        with pytest.raises(CatalogError) as error:
            articles.search(**kwargs)
        assert error.value.status == 422, kwargs


def _day(offset: int) -> str:
    return (datetime.now(UTC).astimezone(ZONE).date() + timedelta(days=offset)).isoformat()
