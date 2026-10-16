import json

from buscador_midias.analysis.replies import json_items

ENTRIES = [{"id": 1, "x": "a"}, {"id": 2, "x": "b"}]


def test_aceita_objeto_com_items():
    assert json_items(json.dumps({"items": ENTRIES})) == ENTRIES


def test_aceita_array_json_puro_na_raiz():
    assert json_items(json.dumps(ENTRIES)) == ENTRIES


def test_aceita_bloco_de_codigo_e_texto_em_volta():
    body = json.dumps(ENTRIES)

    assert json_items("```json\n" + body + "\n```") == ENTRIES
    assert json_items("Aqui está o resultado:\n" + body + "\nEspero ter ajudado.") == ENTRIES


def test_json_invalido_ou_de_outro_formato_devolve_lista_vazia_sem_levantar():
    for text in ("[1, 2", '{"items": "nao é lista"}', "null", "[]", '"texto"', ""):
        assert json_items(text) == []
