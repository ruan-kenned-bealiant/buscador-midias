import json

import httpx

from buscador_midias.triage.openrouter import OpenRouterClassifier
from buscador_midias.triage.relevance import Decision, HeadlineItem

ITEMS = [
    HeadlineItem(id=1, domain="g1.globo.com", text="PF prende empresário por fraude"),
    HeadlineItem(id=2, domain="extra.globo.com", text="Previsão do tempo para sexta"),
]
SECRET = "sk-or-segredo-123"


def completion(*entries: dict, cost: float | None = 0.0001) -> dict:
    body: dict = {
        "choices": [{"message": {"content": json.dumps({"items": list(entries)})}}],
        "usage": {"prompt_tokens": 400, "completion_tokens": 60},
    }
    if cost is not None:
        body["usage"]["cost"] = cost
    return body


GOOD = (
    {"id": 1, "decision": "keep", "themes": ["fiscal"], "reason": "fraude"},
    {"id": 2, "decision": "discard", "themes": [], "reason": "clima"},
)


def make(handler, **kwargs) -> OpenRouterClassifier:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenRouterClassifier(
        client,
        api_key=SECRET,
        model="google/gemini-2.5-flash-lite",
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_chamada_traz_modelo_temperatura_zero_prompt_e_manchetes():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(*GOOD))

    verdicts = make(handler, base_url="https://openrouter.ai/api/v1/").classify(ITEMS)

    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == f"Bearer {SECRET}"
    body = seen["body"]
    assert body["model"] == "google/gemini-2.5-flash-lite"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "1 | g1.globo.com | PF prende empresário por fraude" in body["messages"][1]["content"]
    assert [v.decision for v in verdicts] == [Decision.KEEP, Decision.DISCARD]


def test_uso_e_custo_sao_acumulados_entre_chamadas():
    classifier = make(lambda request: httpx.Response(200, json=completion(*GOOD)))

    classifier.classify(ITEMS)
    classifier.classify(ITEMS)

    usage = classifier.usage
    assert (usage.calls, usage.prompt_tokens, usage.completion_tokens) == (2, 800, 120)
    assert usage.cost_usd == 0.0002


def test_sem_custo_informado_o_total_fica_nulo():
    classifier = make(lambda request: httpx.Response(200, json=completion(*GOOD, cost=None)))

    classifier.classify(ITEMS)

    assert classifier.usage.cost_usd is None


def test_429_e_5xx_sao_repetidos_e_depois_funciona():
    statuses = iter([429, 503])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses, 200)
        if status != 200:
            return httpx.Response(status, text="tente depois")
        return httpx.Response(200, json=completion(*GOOD))

    classifier = make(handler)

    verdicts = classifier.classify(ITEMS)

    assert [v.decision for v in verdicts] == [Decision.KEEP, Decision.DISCARD]
    assert classifier.usage.retries == 2


def test_falha_persistente_deixa_tudo_desconhecido_sem_vazar_a_chave():
    classifier = make(lambda request: httpx.Response(500, text=f"erro interno {SECRET}"))

    verdicts = classifier.classify(ITEMS)

    assert [v.decision for v in verdicts] == [Decision.UNKNOWN, Decision.UNKNOWN]
    assert classifier.usage.errors == 1
    assert SECRET not in (classifier.usage.last_error or "")


def test_erro_de_rede_tambem_vira_desconhecido():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede")

    classifier = make(handler)

    verdicts = classifier.classify(ITEMS)

    assert [v.decision for v in verdicts] == [Decision.UNKNOWN, Decision.UNKNOWN]
    assert classifier.usage.errors == 1


def test_erro_4xx_que_nao_e_429_nao_e_repetido():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text="chave inválida")

    classifier = make(handler)

    classifier.classify(ITEMS)

    assert len(calls) == 1
    assert "401" in (classifier.usage.last_error or "")


def test_resposta_sem_conteudo_vira_desconhecido():
    classifier = make(lambda request: httpx.Response(200, json={"choices": []}))

    verdicts = classifier.classify(ITEMS)

    assert [v.decision for v in verdicts] == [Decision.UNKNOWN, Decision.UNKNOWN]


def test_prompt_de_sistema_pode_ser_trocado():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(*GOOD))

    make(handler, system_prompt="PROMPT ALTERNATIVO").classify(ITEMS)

    assert seen["body"]["messages"][0] == {"role": "system", "content": "PROMPT ALTERNATIVO"}


def test_transporte_generico_devolve_o_texto_e_acumula_o_uso():
    from buscador_midias.triage.openrouter import OpenRouterChat

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(*GOOD))

    chat = OpenRouterChat(
        httpx.Client(transport=httpx.MockTransport(handler)),
        api_key=SECRET,
        model="google/gemini-2.5-flash-lite",
        sleep=lambda _s: None,
    )

    text = chat.complete("SISTEMA", "USUARIO", max_tokens=321)

    assert text is not None and '"items"' in text
    assert seen["body"]["max_tokens"] == 321
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "SISTEMA"},
        {"role": "user", "content": "USUARIO"},
    ]
    assert chat.usage.calls == 1
    assert chat.usage.prompt_tokens == 400


def test_transporte_generico_devolve_none_na_falha_e_registra_o_erro():
    from buscador_midias.triage.openrouter import OpenRouterChat

    chat = OpenRouterChat(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text=SECRET))),
        api_key=SECRET,
        model="m",
        sleep=lambda _s: None,
    )

    assert chat.complete("S", "U", max_tokens=10) is None
    assert chat.usage.errors == 1
    assert SECRET not in (chat.usage.last_error or "")
