"""Parecer por entidade, provas literais e respostas limitadas do provedor."""

import json

import httpx
import pytest

from buscador_midias.analysis.context import AnalysisItem, OpenRouterContextAnalyzer
from buscador_midias.analysis.entity_context import evidence_options, parse_entity_contexts
from buscador_midias.analysis.openrouter import OpenRouterChat

TEXT = "João Silva é investigado por fraude. Maria Souza é a vítima."
ENTITIES = (
    dict(name="João Silva", type="PF", role="investigado"),
    dict(name="Maria Souza", type="PF", role="vitima"),
)


def response(index=0, **changes):
    context = dict(
        alerta=index == 0,
        confianca=0.9,
        justificativa="É investigado" if index == 0 else "É vítima",
        fato_adverso="Investigado por fraude" if index == 0 else "",
        trecho_evidencia=TEXT.split(". ")[index].rstrip(".") + ".",
        themes=["criminal"] if index == 0 else [],
    )
    context.update(changes)
    return dict(ENTITIES[index], context=context)


def test_investigado_e_vitima_na_mesma_noticia_tem_pareceres_distintos():
    contexts = parse_entity_contexts([response(), response(1)], ENTITIES, TEXT, headline_only=False)
    assert len(contexts) == 2
    assert contexts[0].alert is True and contexts[0].adverse_fact
    assert contexts[1].alert is False and contexts[1].adverse_fact == ""
    assert contexts[1].themes == ()


def test_fato_nulo_e_normalizado_apenas_para_alerta_explicitamente_falso():
    [context] = parse_entity_contexts(
        [response(1, fato_adverso=None)], ENTITIES, TEXT, headline_only=False
    )
    assert context.alert is False and context.adverse_fact == ""
    assert not parse_entity_contexts(
        [response(fato_adverso=None)], ENTITIES, TEXT, headline_only=False
    )


@pytest.mark.parametrize(
    "changes",
    [
        dict(trecho_evidencia="João Silva foi condenado."),
        dict(trecho_evidencia="Maria Souza é a vítima."),
        dict(alerta="false"),
        dict(confianca=float("nan")),
        dict(justificativa=""),
    ],
)
def test_contexto_invalido_nao_vira_parecer_negativo_ou_positivo(changes):
    assert not parse_entity_contexts([response(**changes)], ENTITIES, TEXT, headline_only=False)


def test_vitima_nao_pode_herdar_alerta_do_investigado():
    diagnostics = {}
    assert not parse_entity_contexts(
        [response(1, alerta=True, fato_adverso="Fraude")],
        ENTITIES,
        TEXT,
        headline_only=False,
        diagnostics=diagnostics,
    )
    assert diagnostics["incompatible_alert"] == 1
    assert diagnostics["missing"] == 2


def test_resposta_truncada_fica_pendente_e_lote_maior_e_recusado_antes_da_chamada():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"items":['}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        analyzer = OpenRouterContextAnalyzer(OpenRouterChat(client, api_key="test", attempts=1))
        item = AnalysisItem(1, "example.test", "Notícia", "", TEXT, "text")
        assert analyzer.analyze_entities(item, ENTITIES) == ()
        with pytest.raises(ValueError):
            analyzer.analyze_entities(item, list(ENTITIES) * 3)
    assert len(requests) == 1
    assert requests[0]["max_tokens"] == 1600


def test_ancoragem_usa_apenas_recorte_realmente_enviado():
    def handler(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps({"items": [response()]})}}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        analyzer = OpenRouterContextAnalyzer(
            OpenRouterChat(client, api_key="test", attempts=1), max_text_chars=5
        )
        item = AnalysisItem(1, "example.test", "Notícia", "", TEXT, "text")
        assert analyzer.analyze_entities(item, ENTITIES) == ()


@pytest.mark.parametrize("reference,accepted", [(1, 1), (99, 0), (True, 0), ("1", 0)])
def test_referencia_do_trecho_resolve_somente_texto_extraido_da_propria_entidade(
    reference, accepted
):
    received = []

    def handler(request):
        received.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
        entry = response(trecho_id=reference)
        entry["context"].pop("trecho_evidencia")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps({"items": [entry]})}}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        analyzer = OpenRouterContextAnalyzer(OpenRouterChat(client, api_key="test", attempts=1))
        result = analyzer.analyze_entities(AnalysisItem(1, "a", "", "", TEXT, "text"), ENTITIES)
    assert len(result) == accepted
    if result:
        assert result[0].evidence == "João Silva é investigado por fraude."
    for entity in received[0]["entities"]:
        assert all(entry["texto"] in TEXT for entry in entity["evidencias"])


def test_trechos_de_sentenca_longa_tem_tamanho_limitado_e_nao_sao_fabricados():
    source = "Introdução " * 100 + "João Silva foi citado " + "continuação " * 100
    options = evidence_options("João Silva", source)
    assert 1 <= len(options) <= 4
    assert all(len(quote) <= 600 and quote in source for quote in options.values())
    assert evidence_options("Entidade inexistente", source) == {}
