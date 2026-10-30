import json

import httpx
import pytest

from buscador_midias.analysis.openrouter import OpenRouterChat

SECRET = "sk-or-segredo-123"


def completion(*entries: dict, cost: float | None = 0.0001) -> dict:
    body: dict = {
        "choices": [{"message": {"content": json.dumps({"items": list(entries)})}}],
        "usage": {"prompt_tokens": 400, "completion_tokens": 60},
    }
    if cost is not None:
        body["usage"]["cost"] = cost
    return body


ENTRY = {"id": 1, "entities": []}


@pytest.mark.parametrize("reason", ["length", "max_tokens"])
def test_resposta_truncada_mesmo_com_json_valido_nao_vira_analise_completa(reason):
    payload = completion(ENTRY)
    payload["choices"][0]["finish_reason"] = reason
    chat = make(lambda request: httpx.Response(200, json=payload))
    assert chat.complete("S", "U", max_tokens=10) is None
    assert chat.usage.last_error == "resposta_truncada"
    assert chat.usage.calls == 1 and chat.usage.errors == 1
    assert chat.usage.completion_tokens == 60 and chat.usage.cost_usd == 0.0001


def make(handler, **kwargs) -> OpenRouterChat:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenRouterChat(
        client,
        api_key=SECRET,
        model="google/gemini-2.5-flash-lite",
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_chamada_traz_modelo_temperatura_zero_prompt_e_mensagem():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(ENTRY))

    chat = make(handler, base_url="https://openrouter.ai/api/v1/")

    text = chat.complete("SISTEMA", "USUARIO", max_tokens=321)

    assert text is not None and '"items"' in text
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == f"Bearer {SECRET}"
    body = seen["body"]
    assert body["model"] == "google/gemini-2.5-flash-lite"
    assert body["temperature"] == 0
    assert body["max_tokens"] == 321
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "SISTEMA"},
        {"role": "user", "content": "USUARIO"},
    ]


def test_uso_e_custo_sao_acumulados_entre_chamadas():
    chat = make(lambda request: httpx.Response(200, json=completion(ENTRY)))

    chat.complete("S", "U", max_tokens=10)
    chat.complete("S", "U", max_tokens=10)

    usage = chat.usage
    assert (usage.calls, usage.prompt_tokens, usage.completion_tokens) == (2, 800, 120)
    assert usage.cost_usd == 0.0002


def test_sem_custo_informado_o_total_fica_nulo():
    chat = make(lambda request: httpx.Response(200, json=completion(ENTRY, cost=None)))

    chat.complete("S", "U", max_tokens=10)

    assert chat.usage.cost_usd is None


def test_429_e_5xx_sao_repetidos_e_depois_funciona():
    statuses = iter([429, 503])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses, 200)
        if status != 200:
            return httpx.Response(status, text="tente depois")
        return httpx.Response(200, json=completion(ENTRY))

    chat = make(handler)

    assert chat.complete("S", "U", max_tokens=10) is not None
    assert chat.usage.retries == 2


def test_falha_persistente_devolve_none_sem_vazar_a_chave():
    chat = make(lambda request: httpx.Response(500, text=f"erro interno {SECRET}"))

    assert chat.complete("S", "U", max_tokens=10) is None
    assert chat.usage.errors == 1
    assert SECRET not in (chat.usage.last_error or "")


def test_erro_de_rede_tambem_devolve_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede")

    chat = make(handler)

    assert chat.complete("S", "U", max_tokens=10) is None
    assert chat.usage.errors == 1


def test_erro_4xx_que_nao_e_429_nao_e_repetido():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text=SECRET)

    chat = make(handler)

    assert chat.complete("S", "U", max_tokens=10) is None
    assert len(calls) == 1
    assert "401" in (chat.usage.last_error or "")
    assert SECRET not in (chat.usage.last_error or "")


def test_resposta_sem_conteudo_devolve_none():
    chat = make(lambda request: httpx.Response(200, json={"choices": []}))

    assert chat.complete("S", "U", max_tokens=10) is None
    assert chat.usage.errors == 1


def test_resposta_200_sem_conteudo_e_repetida_antes_de_virar_falha():
    # O OpenRouter às vezes devolve 200 com um objeto de erro do provedor de origem.
    bodies = iter([{"error": {"message": "provedor indisponível"}}, {"choices": []}])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(bodies, None) or completion(ENTRY))

    chat = make(handler)

    assert chat.complete("S", "U", max_tokens=10) is not None
    assert chat.usage.retries == 2
    assert chat.usage.errors == 0


def test_resposta_sem_conteudo_persistente_vira_falha_apos_as_tentativas():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"error": {"message": SECRET}})

    chat = make(handler)

    assert chat.complete("S", "U", max_tokens=10) is None
    assert len(calls) > 1
    assert chat.usage.errors == 1
    assert chat.usage.last_error == "resposta sem conteúdo"
    assert SECRET not in (chat.usage.last_error or "")


FALLBACK = "anthropic/claude-haiku-4.5"


def filtered() -> dict:
    """O que o provedor devolve quando o filtro de conteúdo recusa: sem texto e sem tokens."""
    return {
        "choices": [
            {
                "message": {"content": "I am sorry, I cannot assist"},
                "finish_reason": "content_filter",
            }
        ],
        "usage": {"prompt_tokens": 300, "completion_tokens": 0, "cost": 0},
    }


def test_filtro_de_conteudo_sem_modelo_alternativo_devolve_none_com_motivo_proprio():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=filtered())

    chat = make(handler)

    assert chat.complete("S", "U", max_tokens=10) is None
    assert calls == ["google/gemini-2.5-flash-lite"]  # recusa deliberada não se repete
    assert chat.usage.last_error == "filtro_de_conteudo"
    assert chat.usage.errors == 1 and chat.usage.fallbacks == 0


def test_filtro_de_conteudo_repete_uma_vez_com_o_modelo_alternativo():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return httpx.Response(200, json=completion(ENTRY) if model == FALLBACK else filtered())

    chat = make(handler, fallback_model=FALLBACK)

    text = chat.complete("S", "U", max_tokens=10)

    assert text is not None and '"items"' in text
    assert calls == ["google/gemini-2.5-flash-lite", FALLBACK]
    assert chat.answered_model == FALLBACK
    assert chat.usage.fallbacks == 1
    assert chat.usage.errors == 0 and chat.usage.last_error is None
    assert chat.usage.calls == 2  # as duas chamadas gastaram consumo


def test_o_modelo_alternativo_nao_entra_quando_o_principal_responde():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=completion(ENTRY))

    chat = make(handler, fallback_model=FALLBACK)

    assert chat.complete("S", "U", max_tokens=10) is not None
    assert calls == ["google/gemini-2.5-flash-lite"]
    assert chat.answered_model == "google/gemini-2.5-flash-lite"
    assert chat.usage.fallbacks == 0


def test_o_modelo_alternativo_so_serve_ao_filtro_de_conteudo():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(500)

    chat = make(handler, fallback_model=FALLBACK)

    assert chat.complete("S", "U", max_tokens=10) is None
    assert set(calls) == {"google/gemini-2.5-flash-lite"}
    assert chat.usage.fallbacks == 0


def test_se_o_modelo_alternativo_tambem_for_filtrado_a_falha_continua_sendo_o_filtro():
    chat = make(lambda request: httpx.Response(200, json=filtered()), fallback_model=FALLBACK)

    assert chat.complete("S", "U", max_tokens=10) is None
    assert chat.usage.last_error == "filtro_de_conteudo"
    assert chat.usage.fallbacks == 1 and chat.usage.errors == 1
