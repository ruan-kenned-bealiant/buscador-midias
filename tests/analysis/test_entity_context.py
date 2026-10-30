"""Parecer por entidade, provas literais e respostas limitadas do provedor."""

import json

import httpx
import pytest

from buscador_midias.analysis.context import AnalysisItem, OpenRouterContextAnalyzer
from buscador_midias.analysis.entity_context import (
    evidence_options,
    name_in_text,
    parse_entity_contexts,
)
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


KOREAN = "[서울=뉴시스] 이재우 기자 = 첫 미국인 교황인 레오 14세는 우편투표를 신청했다. NBC는 이를 보도했다."
CHINESE = (
    "國際油價今天震盪收低。繼Google財報暴雷之後，亞馬遜同樣不如預期。輝達（NVIDIA）總部落腳北士科。"
)
JAPANESE = "ANAホールディングス（HD）は29日、AIRDOを完全子会社化すると発表した。"


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("NBC", KOREAN),
        ("이재우", KOREAN),
        ("레오 14세", KOREAN),
        ("Google", CHINESE),
        ("輝達（NVIDIA）", CHINESE),
        ("ANAホールディングス（HD）", JAPANESE),
        ("AIRDO", JAPANESE),
    ],
)
def test_nome_colado_a_escrita_sem_espacos_ainda_e_encontrado(name, text):
    assert name_in_text(name, text)
    options = evidence_options(name, text)
    assert options and all(quote in text and len(quote) <= 600 for quote in options.values())


def test_frase_de_escrita_cjk_termina_no_ponto_final_ideografico():
    options = evidence_options("Google", CHINESE)
    assert list(options.values()) == ["繼Google財報暴雷之後，亞馬遜同樣不如預期。"]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Ana", "A Banana caiu."),
        ("Silva", "Silvana foi citada."),
        ("João", "Joãozinho chegou."),
        ("AIRDO", "ANAは AIRDOXと発表した。"),
        ("李", "李明在北京。"),
    ],
)
def test_a_fronteira_de_palavra_continua_valendo_para_escrita_com_espacos_e_nome_curto(name, text):
    assert not name_in_text(name, text)


def test_acentos_continuam_sendo_ignorados():
    assert name_in_text("Joao Silva", "João Silva foi citado.")
