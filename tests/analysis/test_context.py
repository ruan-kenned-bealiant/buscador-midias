import json

import httpx

from buscador_midias.analysis.context import (
    AnalysisItem,
    ContextVerdict,
    OpenRouterContextAnalyzer,
    analyze_all,
    build_user_message,
    parse_analysis,
)
from buscador_midias.analysis.openrouter import OpenRouterChat
from buscador_midias.analysis.prompt import SYSTEM_PROMPT
from buscador_midias.analysis.themes import THEME_SLUGS

ITEMS = [
    AnalysisItem(
        1,
        "g1.globo.com",
        "PF prende empresário",
        "Operação em MG",
        "A PF prendeu João Silva por fraude.",
        "text",
    ),
    AnalysisItem(2, "extra.globo.com", "pf prende empresario", "", "", "headline"),
]


def raw(*entries: dict) -> str:
    return json.dumps({"items": list(entries)})


def entry(item_id: int = 1, **kw) -> dict:
    base = {
        "id": item_id,
        "finding": True,
        "themes": ["fiscal", "criminal"],
        "entities": [{"name": "João Silva", "type": "PF", "role": "acusado"}],
        "adverse_fact": "Preso por fraude em licitação",
        "summary": "A PF prendeu o empresário por fraude.",
        "confidence": 0.9,
    }
    base.update(kw)
    return base


def test_mensagem_traz_cada_item_em_json_de_uma_linha_com_o_texto_truncado():
    long_text = "palavra " * 1000
    items = [AnalysisItem(1, "a.com", "Manchete", "Desc", long_text, "text")]

    lines = build_user_message(items, max_text_chars=200).splitlines()

    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["id"] == 1
    assert data["fonte"] == "a.com"
    assert data["manchete"] == "Manchete"
    assert data["descricao"] == "Desc"
    assert len(data["texto"]) <= 200
    assert data["base"] == "texto"


def test_base_somente_manchete_e_sinalizada_ao_modelo():
    [line] = build_user_message([ITEMS[1]]).splitlines()

    data = json.loads(line)
    assert data["base"] == "somente_manchete"
    assert data["texto"] == ""


def test_texto_com_quebras_de_linha_nao_quebra_o_formato():
    item = AnalysisItem(1, "a.com", "M", "D", "linha um\nlinha dois", "text")

    assert len(build_user_message([item]).splitlines()) == 1


def test_prompt_cita_todos_os_temas_e_o_formato_de_saida():
    for slug in THEME_SLUGS:
        assert slug in SYSTEM_PROMPT
    for key in ("finding", "entities", "adverse_fact", "confidence", "somente_manchete"):
        assert key in SYSTEM_PROMPT


def test_resposta_valida_vira_um_veredito_completo():
    [verdict, _] = parse_analysis(raw(entry()), ITEMS)

    assert verdict == ContextVerdict(
        id=1,
        decided=True,
        finding=True,
        themes=("fiscal", "criminal"),
        entities=({"name": "João Silva", "type": "PF", "role": "acusado"},),
        adverse_fact="Preso por fraude em licitação",
        summary="A PF prendeu o empresário por fraude.",
        confidence=0.9,
        basis="text",
    )


def test_base_do_veredito_vem_do_item_e_nao_do_modelo():
    verdicts = parse_analysis(raw(entry(1), entry(2)), ITEMS)

    assert [v.basis for v in verdicts] == ["text", "headline"]


def test_item_sem_resposta_fica_indeciso():
    verdicts = parse_analysis(raw(entry(1)), ITEMS)

    assert verdicts[1].decided is False
    assert verdicts[1].finding is False


def test_texto_que_nao_e_json_deixa_tudo_indeciso():
    assert [v.decided for v in parse_analysis("desculpe", ITEMS)] == [False, False]


def test_aceita_bloco_de_codigo_e_id_como_texto():
    text = "```json\n" + raw(entry("1")) + "\n```"

    assert parse_analysis(text, ITEMS[:1])[0].decided is True


def test_achado_sem_entidade_nomeada_nao_conta_como_achado():
    verdict = parse_analysis(raw(entry(entities=[])), ITEMS[:1])[0]

    assert verdict.decided is True
    assert verdict.finding is False


def test_entidades_sao_saneadas_tipo_papel_nome_e_limite():
    entities = [{"name": f"Pessoa {n}", "type": "XYZ", "role": "ACUSADO"} for n in range(40)]
    entities.append({"name": "", "type": "PF", "role": "acusado"})
    entities.append("lixo")  # type: ignore[arg-type]

    names = " ".join(f"Pessoa {n}" for n in range(40))
    item = AnalysisItem(1, "a.com", "Manchete", "", f"Acusados: {names}.", "text")

    verdict = parse_analysis(raw(entry(entities=entities)), [item])[0]

    assert len(verdict.entities) == 30
    assert verdict.entities[0] == {"name": "Pessoa 0", "type": "OUTRO", "role": "acusado"}


def test_confianca_e_limitada_ao_intervalo_e_texto_longo_e_cortado():
    verdict = parse_analysis(
        raw(entry(confidence=7, adverse_fact="x" * 900, summary="y" * 900)), ITEMS[:1]
    )[0]

    assert verdict.confidence == 1.0
    assert len(verdict.adverse_fact) <= 300
    assert len(verdict.summary) <= 400
    assert parse_analysis(raw(entry(confidence="abc")), ITEMS[:1])[0].confidence == 0.0
    assert parse_analysis(raw(entry(confidence=-3)), ITEMS[:1])[0].confidence == 0.0


def test_temas_desconhecidos_saem_e_o_maximo_e_tres():
    verdict = parse_analysis(
        raw(entry(themes=["fiscal", "inventado", "criminal", "corrupcao", "ambiental"])), ITEMS[:1]
    )[0]

    assert verdict.themes == ("fiscal", "criminal", "corrupcao")


def test_so_manchete_limita_a_confianca_a_060():
    verdict = parse_analysis(raw(entry(2, confidence=0.95)), ITEMS)[1]

    assert verdict.basis == "headline"
    assert verdict.confidence == 0.6


def chat_for(handler) -> OpenRouterChat:
    return OpenRouterChat(
        httpx.Client(transport=httpx.MockTransport(handler)),
        api_key="k",
        model="google/gemini-2.5-flash-lite",
        sleep=lambda _s: None,
    )


def completion(*entries: dict) -> dict:
    return {
        "choices": [{"message": {"content": raw(*entries)}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 200, "cost": 0.0002},
    }


def test_adaptador_envia_prompt_e_itens_e_devolve_vereditos():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(entry(1), entry(2)))

    analyzer = OpenRouterContextAnalyzer(chat_for(handler))

    verdicts = analyzer.analyze(ITEMS)

    assert seen["body"]["messages"][0]["content"] == SYSTEM_PROMPT
    assert len(seen["body"]["messages"][1]["content"].splitlines()) == 2
    assert [v.decided for v in verdicts] == [True, True]
    assert analyzer.usage.cost_usd == 0.0002


def test_falha_do_provedor_deixa_o_lote_indeciso():
    analyzer = OpenRouterContextAnalyzer(chat_for(lambda r: httpx.Response(500, text="erro")))

    verdicts = analyzer.analyze(ITEMS)

    assert [v.decided for v in verdicts] == [False, False]
    assert analyzer.usage.errors == 1


class FakeAnalyzer:
    def __init__(self) -> None:
        self.batches: list[list[int]] = []

    def analyze(self, items):
        self.batches.append([i.id for i in items])
        return [ContextVerdict(i.id, True, False, (), (), "", "", 0.0, i.basis) for i in items]


def test_analyze_all_divide_em_lotes_e_preserva_a_ordem():
    items = [AnalysisItem(n, "a.com", f"m{n}", "", "", "text") for n in range(1, 8)]
    fake = FakeAnalyzer()

    verdicts = analyze_all(fake, items, batch_size=3, workers=1)

    assert fake.batches == [[1, 2, 3], [4, 5, 6], [7]]
    assert [v.id for v in verdicts] == list(range(1, 8))


def test_analyze_all_lote_que_explode_fica_indeciso_sem_derrubar_os_outros():
    class Flaky(FakeAnalyzer):
        def analyze(self, items):
            if items[0].id == 4:
                raise RuntimeError("boom")
            return super().analyze(items)

    items = [AnalysisItem(n, "a.com", f"m{n}", "", "", "text") for n in range(1, 7)]

    verdicts = analyze_all(Flaky(), items, batch_size=3, workers=1)

    assert [v.decided for v in verdicts] == [True] * 3 + [False] * 3


def test_aceita_array_json_puro_na_raiz():
    text = json.dumps([entry(1), entry(2)])

    verdicts = parse_analysis(text, ITEMS)

    assert [v.decided for v in verdicts] == [True, True]


def test_array_puro_em_bloco_de_codigo():
    text = "```json\n" + json.dumps([entry(1)]) + "\n```"

    assert parse_analysis(text, ITEMS[:1])[0].decided is True


def test_achado_exige_entidade_com_papel_adverso():
    for role in ("autoridade", "citado", "vitima", "testemunha", "outro"):
        entities = [{"name": "João Silva", "type": "PF", "role": role}]

        verdict = parse_analysis(raw(entry(entities=entities)), ITEMS[:1])[0]

        assert verdict.decided is True
        assert verdict.finding is False, role
        assert verdict.entities[0]["name"] == "João Silva"  # a entidade continua registrada


def test_papeis_adversos_sao_aceitos_sem_acento_e_em_qualquer_caixa():
    for role in ("acusado", "INVESTIGADO", "condenado", "réu", "Responsável", "reu", "responsavel"):
        entities = [{"name": "João Silva", "type": "PF", "role": role}]

        verdict = parse_analysis(raw(entry(entities=entities)), ITEMS[:1])[0]

        assert verdict.finding is True, role


def test_entidade_que_nao_aparece_no_texto_fornecido_e_descartada():
    entities = [
        {"name": "Polícia Civil", "type": "OUTRO", "role": "autoridade"},
        {"name": "Fulano Inventado", "type": "PF", "role": "acusado"},
        {"name": "João Silva", "type": "PF", "role": "acusado"},
    ]

    verdict = parse_analysis(raw(entry(entities=entities)), ITEMS[:1])[0]

    assert [e["name"] for e in verdict.entities] == ["João Silva"]
    assert verdict.finding is True


def test_se_so_sobram_entidades_inventadas_nao_ha_achado():
    entities = [{"name": "Fulano Inventado", "type": "PF", "role": "acusado"}]

    verdict = parse_analysis(raw(entry(entities=entities)), ITEMS[:1])[0]

    assert verdict.entities == ()
    assert verdict.finding is False


def test_ancoragem_ignora_acento_e_caixa_e_confere_palavra_por_palavra():
    item = AnalysisItem(
        1, "a.com", "Manchete", "", "Operação prendeu o EMPRESÁRIO joao da silva em Minas.", "text"
    )
    ok = parse_analysis(
        raw(entry(entities=[{"name": "João Silva", "type": "PF", "role": "acusado"}])), [item]
    )[0]
    out_of_order = parse_analysis(
        raw(entry(entities=[{"name": "Silva João", "type": "PF", "role": "acusado"}])), [item]
    )[0]
    partial = parse_analysis(
        raw(entry(entities=[{"name": "João Pedro Silva", "type": "PF", "role": "acusado"}])), [item]
    )[0]

    assert ok.finding is True
    assert out_of_order.finding is True
    assert partial.finding is False  # "Pedro" não está no texto


def test_prompt_define_o_papel_responsavel_e_a_exigencia_de_acusado_nomeado():
    assert "responsavel" in SYSTEM_PROMPT
    assert "nomead" in SYSTEM_PROMPT.lower()
    assert "autoridade" in SYSTEM_PROMPT


def test_prompt_pede_entidades_em_toda_materia_mesmo_sem_fato_adverso():
    text = SYSTEM_PROMPT.lower()

    assert "mesmo quando finding = false" in text  # a lista de entidades não depende do achado
    assert "sem fato adverso" in text and "continua valendo" in text
    assert "países" in text and "cidades" in text  # lugares não são entidades


def test_versao_do_prompt_acompanha_a_mudanca_de_criterio():
    from buscador_midias.analysis.prompt import PROMPT_VERSION

    assert PROMPT_VERSION == "context-2026-10-26.3-entity-context-refs"


def test_new_categories_are_validated_without_discarding_adverse_facts():
    from buscador_midias.analysis.classification import CONTENT_CATEGORIES

    for category in CONTENT_CATEGORIES:
        verdict = parse_analysis(raw(entry(content_category=category)), ITEMS[:1])[0]
        assert verdict.content_category == category
        assert verdict.finding and verdict.entities
    for value in [None, "", [], {}, "invented", 123]:
        verdict = parse_analysis(raw(entry(content_category=value)), ITEMS[:1])[0]
        assert verdict.content_category is None
    assert (
        parse_analysis(raw(entry(content_category=" OPINION ")), ITEMS[:1])[0].content_category
        == "opinion"
    )


def test_editorial_country_requires_literal_evidence_not_an_invented_quote():
    item = AnalysisItem(
        1,
        "example.test",
        "Edição brasileira",
        "",
        "Edição brasileira do Jornal Exemplo. Economia da China desacelera.",
        "text",
    )
    result = dict(
        content_category="news",
        article_country="BR",
        article_country_evidence="Edição brasileira do Jornal Exemplo.",
    )
    verdict = parse_analysis(raw(entry(**result)), [item])[0]
    assert verdict.article_country == "BR"
    assert verdict.article_country_evidence == result["article_country_evidence"]
    for country, evidence in [("CN", ""), ("CN", "Editorial chinês"), ("ZZ", "Economia da China")]:
        verdict = parse_analysis(
            raw(entry(article_country=country, article_country_evidence=evidence)), [item]
        )[0]
        assert verdict.article_country is None


def test_prompt_explains_origin_not_subject_and_content_not_url():
    assert "nunca pela" in SYSTEM_PROMPT
    assert "Não use o país" in SYSTEM_PROMPT
    assert "nacionalidade de uma pessoa" in SYSTEM_PROMPT
    for name in ["weather", "cooking", "horoscope", "opinion"]:
        assert name in SYSTEM_PROMPT
