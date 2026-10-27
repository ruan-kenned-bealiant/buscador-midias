# ruff: noqa: F811 -- fixtures importadas de test_writer_pg são reutilizadas por nome.
"""Retroativo: linhas de contexto sem tema ganham o tema da notícia, sem chamar a IA."""

from __future__ import annotations

import pytest
from test_writer_pg import (  # noqa: F401 - fixtures e helpers do writer
    rows,
    schema,
    themed_article,
    unthemed,
    writer,
)

from buscador_midias.corpus.theme_backfill import backfill_themes

pytestmark = pytest.mark.usefixtures("schema")


def theme_ids(writer, name):
    return [
        r[0]
        for r in rows(
            writer,
            "SELECT theme_id FROM midias_ia_article_contexts WHERE entity_name=%s "
            "ORDER BY theme_id NULLS FIRST",
            (name,),
        )
    ]


def seed_unthemed_with_keywords(writer):
    """Linhas sem tema (gravadas antes da feature) de um artigo cujas palavras-chave têm tema."""
    [outcome] = writer.write([unthemed(themed_article())])
    writer._connection.execute(
        "UPDATE midias_ia_article_keywords SET category='corrupcao',keyword='corrupção' "
        "WHERE article_id=%s",
        (outcome.article_id,),
    )
    writer._connection.commit()
    return outcome


def test_dry_run_reports_and_changes_nothing(writer):
    seed_unthemed_with_keywords(writer)
    report = backfill_themes(writer._connection)
    assert report["rows"] == 3 and report["themed_rows"] == 6 and report["applied"] == 0
    assert theme_ids(writer, "PCC") == [None]


def test_apply_replaces_each_unthemed_row_with_the_themed_rows(writer):
    seed_unthemed_with_keywords(writer)
    report = backfill_themes(writer._connection, apply=True)
    assert report["applied"] == 3
    assert theme_ids(writer, "Maria Virgínia") == [1, 15]
    assert theme_ids(writer, "PCC") == [2, 16]
    assert theme_ids(writer, "Zé Silva") == [1, 15]
    assert rows(
        writer, "SELECT count(*) FROM midias_ia_article_contexts WHERE theme_id IS NULL"
    ) == [(0,)]


def test_apply_keeps_the_individual_verdict_and_fills_the_theme_columns(writer):
    seed_unthemed_with_keywords(writer)
    backfill_themes(writer._connection, apply=True)
    assert rows(
        writer,
        "SELECT alerta,theme,theme_description IS NOT NULL,justificativa,entity_hash "
        "FROM midias_ia_article_contexts WHERE entity_name='Maria Virgínia' ORDER BY theme_id",
    )[0][:4] == (True, "Corrupção", True, "Investigada")


def test_apply_is_idempotent(writer):
    seed_unthemed_with_keywords(writer)
    backfill_themes(writer._connection, apply=True)
    again = backfill_themes(writer._connection, apply=True)
    assert again["rows"] == 0 and again["applied"] == 0
    assert rows(writer, "SELECT count(*) FROM midias_ia_article_contexts") == [(6,)]


def test_articles_without_a_catalog_theme_are_left_alone(writer):
    writer.write([unthemed(themed_article())])
    report = backfill_themes(writer._connection, apply=True)
    assert report["rows"] == 3 and report["themed_rows"] == 0 and report["applied"] == 0
    assert theme_ids(writer, "PCC") == [None]


def test_rows_of_other_writers_and_client_rows_are_not_touched(writer):
    seed_unthemed_with_keywords(writer)
    writer._connection.execute(
        "UPDATE midias_ia_article_contexts SET raw_context=json_build_object('producer','kyx'::text) "
        "WHERE entity_name='PCC'"
    )
    writer._connection.execute(
        "UPDATE midias_ia_article_contexts SET process_id=7 WHERE entity_name='Zé Silva'"
    )
    writer._connection.commit()
    backfill_themes(writer._connection, apply=True)
    assert theme_ids(writer, "PCC") == [None]
    assert theme_ids(writer, "Zé Silva") == [None]
    assert theme_ids(writer, "Maria Virgínia") == [1, 15]


def test_a_failure_in_the_middle_rolls_the_whole_batch_back(writer):
    seed_unthemed_with_keywords(writer)
    writer._connection.execute(
        "ALTER TABLE midias_ia_article_contexts ADD CHECK(theme_id IS NULL OR theme_id<>16)"
    )
    writer._connection.commit()
    with pytest.raises(Exception):  # noqa: B017 - qualquer erro do banco desfaz o lote
        backfill_themes(writer._connection, apply=True)
    assert theme_ids(writer, "Maria Virgínia") == [None]
    assert theme_ids(writer, "PCC") == [None]
