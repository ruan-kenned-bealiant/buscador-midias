"""Testes do gravador contra um Postgres real, em um schema temporário (somem ao final).

Precisam das variáveis ``DB_MIDIAS_IA_*`` e de um banco com as tabelas ``midias_ia_*``; sem isso são
pulados. As tabelas do schema temporário são cópias das reais; o corpus de verdade nunca é tocado.
"""

import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from buscador_midias.analysis.entity_context import EntityContext
from buscador_midias.corpus.article import (
    CorpusArticle,
    CorpusEntity,
    CorpusEvidence,
    CorpusKeyword,
)
from buscador_midias.corpus.contexts import entity_identity
from buscador_midias.corpus.writer import CorpusSchemaError, PostgresCorpusWriter

psycopg = pytest.importorskip("psycopg")
NEEDED = (
    "DB_MIDIAS_IA_HOST",
    "DB_MIDIAS_IA_PORT",
    "DB_MIDIAS_IA_DATABASE",
    "DB_MIDIAS_IA_USERNAME",
    "DB_MIDIAS_IA_PASSWORD",
)
pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in NEEDED), reason="sem variáveis DB_MIDIAS_IA_*"
)
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def schema():
    connection = psycopg.connect(
        host=os.environ["DB_MIDIAS_IA_HOST"],
        port=os.environ["DB_MIDIAS_IA_PORT"],
        dbname=os.environ["DB_MIDIAS_IA_DATABASE"],
        user=os.environ["DB_MIDIAS_IA_USERNAME"],
        password=os.environ["DB_MIDIAS_IA_PASSWORD"],
        autocommit=True,
    )
    name = f"bm_test_{uuid.uuid4().hex[:10]}"
    try:
        connection.execute(f"CREATE SCHEMA {name}")
        for table in (
            "midias_ia_articles",
            "midias_ia_article_entities",
            "midias_ia_article_keywords",
            "midias_ia_article_contexts",
        ):
            connection.execute(f"CREATE TABLE {name}.{table} (LIKE public.{table} INCLUDING ALL)")
            connection.execute(
                f"CREATE SEQUENCE {name}.{table}_seq"
            )  # não consome a sequência real
            connection.execute(
                f"ALTER TABLE {name}.{table} ALTER COLUMN id SET DEFAULT nextval('{name}.{table}_seq')"
            )
        connection.execute(
            f"CREATE TABLE {name}.midias_ia_trusted_sources "
            "(url_without_https_www text, name text, active boolean, country_code text)"
        )
        connection.execute(
            f"INSERT INTO {name}.midias_ia_trusted_sources VALUES "
            "('correio24horas.com.br', 'Correio', true, 'BR'), ('inativo.com', 'Inativo', false, 'US')"
        )
    except psycopg.Error as error:  # banco sem as tabelas: não há o que testar
        connection.close()
        pytest.skip(f"banco indisponível para o teste: {error}")
    yield name
    connection.execute(f"DROP SCHEMA {name} CASCADE")
    connection.close()


@pytest.fixture
def writer(schema: str):
    instance = PostgresCorpusWriter.from_env(os.environ, schema=schema)
    yield instance
    instance.close()


def make_article(**kw) -> CorpusArticle:
    base = {
        "url": "https://www.correio24horas.com.br/brasil/delegada-e-presa-1026",
        "url_hash": "ab" * 32,
        "title": "Delegada é presa",
        "summary": "Resumo",
        "domain": "correio24horas.com.br",
        "source_domain": "correio24horas.com.br",
        "source_name": "Correio",
        "source_country": "BR",
        "source_tier": "trusted",
        "language": "pt-BR",
        "published_at": NOW,
        "description": "Desc",
        "body": "Texto da matéria",
        "ai_verdict": "relevante",
        "ai_score": 0.9,
        "ai_reason": "Delegada presa por vazar informações",
        "ai_summary": "Resumo",
        "ai_model": "google/gemini-2.5-flash-lite",
        "first_seen_by": "buscador-midias",
        "origin": "crawler",
        "evidence": CorpusEvidence("captured", "midias-ia/evidencias/ab.html", "html", 1),
        "entities": (
            CorpusEntity(
                "Maria Virgínia", "maria virginia", "PER", "investigado", "Delegada presa"
            ),
            CorpusEntity("PCC", "pcc", "ORG", "mencionado", None),
        ),
        "keywords": (
            CorpusKeyword("criminal", "criminal", "analysis", 0.9),
            CorpusKeyword("pld/ft", "pldft", "analysis", 0.9),
        ),
    }
    base.update(kw)
    return CorpusArticle(**base)


def rows(writer: PostgresCorpusWriter, sql: str, params=()) -> list[tuple]:
    with writer._connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def contextual_article():
    return make_article(
        entity_contexts=(
            EntityContext(
                "Maria Virgínia",
                "PF",
                "investigado",
                True,
                0.9,
                "A delegada é investigada",
                "Investigada por vazamento",
                "Maria Virgínia é investigada por vazamento",
                ("criminal",),
            ),
            EntityContext(
                "PCC",
                "PJ",
                "citado",
                False,
                0.8,
                "Apenas citado neste trecho",
                "",
                "O PCC é citado na investigação",
            ),
        )
    )


def test_contextos_individuais_sao_atomicos_idempotentes_e_sem_vinculo_de_cliente(writer):
    article = contextual_article()
    [first] = writer.write([article])
    [second] = writer.write([article])
    assert first.error is None and second.error is None
    assert first.contexts == second.contexts == 2
    records = rows(
        writer,
        "SELECT entity_name,alerta,fato_adverso,process_id,client_id,group_id,"
        "theme_id,tema_confirmado,entity_document,raw_context->>'identity_basis',"
        "raw_context->>'evidence' FROM midias_ia_article_contexts ORDER BY entity_name",
    )
    assert len(records) == 2
    assert records[0][:3] == ("Maria Virgínia", True, "Investigada por vazamento")
    assert records[1][:3] == ("PCC", False, "")
    assert records[0][3:9] == (None,) * 6
    assert records[0][9:] == ("name_only", "Maria Virgínia é investigada por vazamento")


@pytest.mark.parametrize("process_id,producer", [(77, "buscador-midias"), (None, "kyx")])
def test_contexto_de_outro_escritor_e_contexto_tematico_nao_sao_sobrescritos(
    writer, process_id, producer
):
    article = contextual_article()
    [outcome] = writer.write([article])
    assert outcome.error is None
    kind, key = entity_identity("Maria Virgínia", "PF")
    writer._connection.execute(
        "UPDATE midias_ia_article_contexts SET process_id=%s,alerta=false,"
        "justificativa='Parecer preservado',raw_context=json_build_object('producer',%s::text) "
        "WHERE entity_hash=%s",
        (process_id, producer, key),
    )
    writer._connection.execute(
        "INSERT INTO midias_ia_article_contexts(article_id,theme_id,entity_name,entity_type,"
        "entity_hash,alerta,created_at,updated_at) VALUES(%s,12,%s,%s,%s,false,now(),now())",
        (outcome.article_id, "Maria Virgínia", kind, key),
    )
    writer._connection.commit()
    [repeated] = writer.write([article])
    assert repeated.error is None and repeated.contexts == 1
    assert rows(
        writer,
        "SELECT justificativa,alerta FROM midias_ia_article_contexts "
        "WHERE entity_hash=%s AND theme_id IS NULL",
        (key,),
    ) == [("Parecer preservado", False)]
    assert rows(writer, "SELECT alerta FROM midias_ia_article_contexts WHERE theme_id=12") == [
        (False,)
    ]


def test_falha_de_contexto_desfaz_artigo_entidades_e_contextos_da_transacao(writer):
    writer._connection.execute(
        "ALTER TABLE midias_ia_article_contexts ADD CHECK(entity_name<>'PCC')"
    )
    writer._connection.commit()
    [outcome] = writer.write([contextual_article()])
    assert outcome.error == "CheckViolation" and outcome.article_id is None
    assert rows(writer, "SELECT count(*) FROM midias_ia_articles") == [(0,)]
    assert rows(writer, "SELECT count(*) FROM midias_ia_article_entities") == [(0,)]
    assert rows(writer, "SELECT count(*) FROM midias_ia_article_contexts") == [(0,)]


def test_regravacao_legada_nao_inventa_nem_apaga_contextos(writer):
    [first] = writer.write([contextual_article()])
    assert first.error is None
    [second] = writer.write([replace(contextual_article(), entity_contexts=())])
    assert second.error is None
    assert rows(writer, "SELECT count(*) FROM midias_ia_article_contexts") == [(2,)]


def test_contexto_preserva_versao_da_analise_e_resumo_individual(writer):
    [outcome] = writer.write([replace(contextual_article(), context_prompt_version="version-old")])
    assert outcome.error is None
    assert rows(
        writer,
        "SELECT raw_context->>'prompt_version',ai_summary "
        "FROM midias_ia_article_contexts ORDER BY entity_name",
    ) == [
        ("version-old", "A delegada é investigada"),
        ("version-old", "Apenas citado neste trecho"),
    ]


def test_schema_com_as_colunas_esperadas_passa_na_checagem(writer):
    writer.check_schema()


def test_checagem_aponta_a_coluna_que_sumiu(writer, schema):
    writer._connection.execute(f"ALTER TABLE {schema}.midias_ia_articles DROP COLUMN ai_summary")
    writer._connection.commit()

    with pytest.raises(CorpusSchemaError, match="ai_summary"):
        writer.check_schema()


def test_artigo_novo_e_gravado_no_formato_do_kyx(writer):
    [outcome] = writer.write([make_article()])

    assert outcome.created is True and outcome.error is None
    [row] = rows(
        writer,
        "SELECT url, titulo, resumo, domain, source_domain, source_name, source_country, source_tier, origin,"
        " first_seen_by, language, ai_verdict, ai_score, ai_reason, ai_summary, ai_model, ai_classified,"
        " evidencia_path, evidencia_status, evidencia_tipo, evidencia_attempts, body, body_source,"
        " description, published_at, created_at IS NOT NULL, collected_at IS NOT NULL"
        " FROM midias_ia_articles WHERE url_hash = %s",
        ("ab" * 32,),
    )
    assert row[:9] == (
        "https://www.correio24horas.com.br/brasil/delegada-e-presa-1026",
        "Delegada é presa",
        "Resumo",
        "correio24horas.com.br",
        "correio24horas.com.br",
        "Correio",
        "BR",
        "trusted",
        "crawler",
    )
    assert row[9:17] == (
        "buscador-midias",
        "pt-BR",
        "relevante",
        Decimal("0.9000"),
        "Delegada presa por vazar informações",
        "Resumo",
        "google/gemini-2.5-flash-lite",
        True,
    )
    assert row[17:21] == ("midias-ia/evidencias/ab.html", "captured", "html", 1)
    assert row[21:24] == ("Texto da matéria", "crawler_html", "Desc")
    assert row[24] == NOW and row[25] is True and row[26] is True


def test_entidades_e_palavras_chave_sao_gravadas(writer):
    writer.write([make_article()])

    entities = rows(
        writer,
        "SELECT e.name, e.name_norm, e.entity_type, e.role, e.context, e.extractor FROM midias_ia_article_entities e"
        " ORDER BY e.name",
    )
    keywords = rows(
        writer,
        "SELECT keyword, category, field, weight FROM midias_ia_article_keywords ORDER BY keyword",
    )
    assert entities == [
        (
            "Maria Virgínia",
            "maria virginia",
            "PER",
            "investigado",
            "Delegada presa",
            "buscador-midias",
        ),
        ("PCC", "pcc", "ORG", "mencionado", None, "buscador-midias"),
    ]
    assert keywords == [
        ("criminal", "criminal", "analysis", Decimal("0.9000")),
        ("pld/ft", "pldft", "analysis", Decimal("0.9000")),
    ]


def test_regravar_o_mesmo_artigo_nao_duplica_nada(writer):
    writer.write([make_article()])

    [outcome] = writer.write([make_article()])

    assert outcome.created is False
    assert rows(writer, "SELECT COUNT(*) FROM midias_ia_articles") == [(1,)]
    assert rows(writer, "SELECT COUNT(*) FROM midias_ia_article_entities") == [(2,)]
    assert rows(writer, "SELECT COUNT(*) FROM midias_ia_article_keywords") == [(2,)]


def test_artigo_nosso_acompanha_a_evidencia_e_troca_entidades_sem_duplicar(writer):
    pending = make_article(evidence=CorpusEvidence("pending", None, None, 0))
    writer.write([pending])

    captured = make_article(
        entities=(CorpusEntity("Maria Virgínia", "maria virginia", "PER", "acusado", "fato"),),
    )
    [outcome] = writer.write([captured])

    assert outcome.evidence_updated is True
    assert rows(
        writer, "SELECT evidencia_status, evidencia_path, evidencia_tipo FROM midias_ia_articles"
    ) == [("captured", "midias-ia/evidencias/ab.html", "html")]
    assert rows(writer, "SELECT name_norm, role FROM midias_ia_article_entities") == [
        ("maria virginia", "acusado")
    ]


def test_artigo_de_outra_origem_nao_e_sobrescrito_so_ganha_evidencia_que_faltava(writer):
    writer._connection.execute(
        "INSERT INTO midias_ia_articles (url, url_hash, titulo, origin, evidencia_status, evidencia_attempts)"
        " VALUES (%s, %s, %s, 'live_search', 'pending', 0)",
        ("https://x.com/a", "ab" * 32, "Título do KyX"),
    )
    writer._connection.commit()

    [outcome] = writer.write([make_article(title="Título nosso")])

    assert outcome.created is False and outcome.evidence_updated is True
    assert rows(
        writer, "SELECT titulo, origin, evidencia_status, evidencia_path FROM midias_ia_articles"
    ) == [("Título do KyX", "live_search", "captured", "midias-ia/evidencias/ab.html")]
    assert rows(writer, "SELECT COUNT(*) FROM midias_ia_article_entities") == [
        (0,)
    ]  # entidades do outro dono intactas


def test_evidencia_ja_capturada_por_outro_nao_e_trocada(writer):
    writer._connection.execute(
        "INSERT INTO midias_ia_articles (url, url_hash, titulo, origin, evidencia_status, evidencia_path, evidencia_attempts)"
        " VALUES (%s, %s, %s, 'imported_corpus', 'captured', 'caminho/do/kyx.html', 0)",
        ("https://x.com/a", "ab" * 32, "T"),
    )
    writer._connection.commit()

    [outcome] = writer.write([make_article()])

    assert outcome.evidence_updated is False
    assert rows(writer, "SELECT evidencia_path FROM midias_ia_articles") == [
        ("caminho/do/kyx.html",)
    ]


def test_falha_em_um_artigo_nao_derruba_os_outros_e_desfaz_so_aquele(writer):
    bad = make_article(
        url_hash="cd" * 32, url="https://x.com/ruim", ai_score=123456.0
    )  # numeric(5,4) estoura
    good = make_article()

    outcomes = writer.write([bad, good])

    assert outcomes[0].error is not None and outcomes[0].created is False
    assert outcomes[1].error is None and outcomes[1].created is True
    assert rows(writer, "SELECT url_hash FROM midias_ia_articles") == [("ab" * 32,)]
    assert rows(writer, "SELECT COUNT(*) FROM midias_ia_article_entities") == [
        (2,)
    ]  # sem sobras do ruim


def test_bytes_nulos_no_texto_sao_removidos_em_vez_de_quebrar_a_gravacao(writer):
    [outcome] = writer.write([make_article(title="Com\x00nulo", body="a\x00b")])

    assert outcome.error is None
    assert rows(writer, "SELECT titulo, body FROM midias_ia_articles") == [("Comnulo", "ab")]


def test_existing_devolve_so_os_hashes_presentes(writer):
    writer.write([make_article()])

    assert writer.existing(["ab" * 32, "ef" * 32]) == {"ab" * 32}
    assert writer.existing([]) == set()


def test_nomes_das_fontes_confiaveis_ativas_por_dominio(writer):
    assert writer.source_names() == {"correio24horas.com.br": "Correio"}


def test_lote_grande_em_uma_chamada_grava_todos(writer):
    batch = [make_article(url_hash=f"{n:064x}", url=f"https://x.com/{n}") for n in range(30)]

    outcomes = writer.write(batch)

    assert sum(o.created for o in outcomes) == 30
    assert rows(writer, "SELECT COUNT(*) FROM midias_ia_articles") == [(30,)]


def test_classification_country_provenance_and_registry_are_persisted(writer):
    from dataclasses import replace

    article = replace(
        make_article(),
        content_category="weather",
        source_country="PT",
        source_country_method="article",
    )
    result = writer.write([article])[0]
    assert not result.error
    assert rows(
        writer,
        "SELECT content_category,source_country,source_country_method FROM midias_ia_articles WHERE id=%s",
        (result.article_id,),
    )[0] == ("weather", "PT", "article")
    assert writer.source_countries() == {"correio24horas.com.br": "BR"}
    writer.write([replace(article, content_category="news")])
    assert (
        rows(
            writer,
            "SELECT content_category FROM midias_ia_articles WHERE id=%s",
            (result.article_id,),
        )[0][0]
        == "weather"
    )


def test_unknown_existing_category_is_filled_even_for_another_origin(writer):
    from dataclasses import replace

    original = replace(make_article(), origin="live_search", content_category=None)
    result = writer.write([original])[0]
    assert not result.error
    updated = writer.write([replace(original, origin="crawler", content_category="opinion")])[0]
    assert not updated.error
    assert (
        rows(
            writer,
            "SELECT content_category FROM midias_ia_articles WHERE id=%s",
            (result.article_id,),
        )[0][0]
        == "opinion"
    )
