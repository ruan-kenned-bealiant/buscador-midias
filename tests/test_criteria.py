import itertools
import json
import sqlite3

import pytest

from buscador_midias.criteria import (
    HAS_ENTITIES_SQL,
    MEETS_CRITERIA_SQL,
    has_entities,
    meets_criteria,
)

PERSON = {"name": "João Silva", "type": "PF", "role": "citado"}


def test_materia_lida_com_entidade_atende():
    assert meets_criteria(basis="text", entities=[PERSON]) is True


def test_materia_lida_sem_entidade_nao_atende():
    assert meets_criteria(basis="text", entities=[]) is False


def test_analise_so_pela_manchete_nao_atende_mesmo_com_entidade():
    assert meets_criteria(basis="headline", entities=[PERSON]) is False


def test_entidade_sem_nome_nao_conta():
    assert has_entities([{"name": "", "type": "PF", "role": "citado"}]) is False
    assert has_entities([{"type": "PF", "role": "citado"}]) is False
    assert has_entities([PERSON]) is True


@pytest.mark.parametrize(
    ("basis", "entities"),
    list(itertools.product(["text", "headline"], [[], [PERSON], [PERSON, PERSON]])),
)
def test_a_regra_em_sql_concorda_com_a_regra_em_python(basis, entities):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE url_analysis (basis TEXT, entities TEXT)")
    conn.execute("INSERT INTO url_analysis VALUES (?, ?)", (basis, json.dumps(entities)))

    in_sql = conn.execute(f"SELECT COUNT(*) FROM url_analysis a WHERE {MEETS_CRITERIA_SQL}")
    has_sql = conn.execute(f"SELECT COUNT(*) FROM url_analysis a WHERE {HAS_ENTITIES_SQL}")

    assert bool(in_sql.fetchone()[0]) is meets_criteria(basis=basis, entities=entities)
    assert bool(has_sql.fetchone()[0]) is has_entities(entities)
