import itertools

import pytest
from psycopg.types.json import Jsonb

from buscador_midias.criteria import (
    HAS_ENTITIES_SQL,
    MEETS_CRITERIA_SQL,
    has_entities,
    meets_criteria,
)
from buscador_midias.state.store import StateStore

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
    store = StateStore()

    row = store._one(
        f"SELECT {MEETS_CRITERIA_SQL}, {HAS_ENTITIES_SQL}"
        " FROM (SELECT %s::varchar AS basis, %s::jsonb AS entities) a",
        (basis, Jsonb(entities)),
    )

    assert row == (meets_criteria(basis=basis, entities=entities), has_entities(entities))
