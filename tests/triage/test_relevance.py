import json

from buscador_midias.triage.prompt import SYSTEM_PROMPT, THEME_SLUGS, build_user_message
from buscador_midias.triage.relevance import (
    Decision,
    HeadlineItem,
    HeadlineVerdict,
    classify_all,
    parse_verdicts,
)

ITEMS = [
    HeadlineItem(id=1, domain="g1.globo.com", text="PF prende empresário por fraude"),
    HeadlineItem(id=2, domain="extra.globo.com", text="Previsão do tempo para sexta"),
]


def raw(*entries: dict) -> str:
    return json.dumps({"items": list(entries)})


def test_mensagem_lista_uma_manchete_por_linha_com_id_e_dominio():
    message = build_user_message(ITEMS)

    assert message.splitlines() == [
        "1 | g1.globo.com | PF prende empresário por fraude",
        "2 | extra.globo.com | Previsão do tempo para sexta",
    ]


def test_manchete_com_quebra_de_linha_nao_quebra_o_formato():
    item = HeadlineItem(id=3, domain="a.com", text="linha um\nlinha dois | com barra")

    assert build_user_message([item]) == "3 | a.com | linha um linha dois / com barra"


def test_prompt_menciona_todos_os_temas_e_a_regra_da_duvida():
    for slug in THEME_SLUGS:
        assert slug in SYSTEM_PROMPT
    assert "keep" in SYSTEM_PROMPT
    assert "discard" in SYSTEM_PROMPT


def test_resposta_valida_vira_um_veredito_por_manchete():
    text = raw(
        {"id": 1, "decision": "keep", "themes": ["fiscal", "criminal"], "reason": "fraude"},
        {"id": 2, "decision": "discard", "themes": [], "reason": "clima"},
    )

    verdicts = parse_verdicts(text, ITEMS)

    assert verdicts == [
        HeadlineVerdict(1, Decision.KEEP, ("fiscal", "criminal"), "fraude"),
        HeadlineVerdict(2, Decision.DISCARD, (), "clima"),
    ]


def test_aceita_resposta_dentro_de_bloco_de_codigo():
    text = "```json\n" + raw({"id": 1, "decision": "keep", "themes": [], "reason": "x"}) + "\n```"

    verdicts = parse_verdicts(text, ITEMS[:1])

    assert verdicts[0].decision is Decision.KEEP


def test_item_ausente_ou_decisao_invalida_fica_desconhecido_e_nunca_descarta():
    text = raw({"id": 1, "decision": "talvez", "themes": [], "reason": ""})

    verdicts = parse_verdicts(text, ITEMS)

    assert [v.decision for v in verdicts] == [Decision.UNKNOWN, Decision.UNKNOWN]


def test_texto_que_nao_e_json_deixa_tudo_desconhecido():
    verdicts = parse_verdicts("desculpe, não consigo", ITEMS)

    assert [v.decision for v in verdicts] == [Decision.UNKNOWN, Decision.UNKNOWN]
    assert [v.id for v in verdicts] == [1, 2]


def test_ids_extras_sao_ignorados_e_o_primeiro_de_um_id_repetido_vence():
    text = raw(
        {"id": 1, "decision": "keep", "themes": [], "reason": "a"},
        {"id": 1, "decision": "discard", "themes": [], "reason": "b"},
        {"id": 99, "decision": "discard", "themes": [], "reason": "c"},
    )

    verdicts = parse_verdicts(text, ITEMS[:1])

    assert verdicts == [HeadlineVerdict(1, Decision.KEEP, (), "a")]


def test_temas_desconhecidos_e_razao_gigante_sao_saneados():
    text = raw(
        {"id": 1, "decision": "keep", "themes": ["fiscal", "inventado"], "reason": "x" * 500}
    )

    verdict = parse_verdicts(text, ITEMS[:1])[0]

    assert verdict.themes == ("fiscal",)
    assert len(verdict.reason) <= 200


def test_id_como_texto_numerico_tambem_e_aceito():
    text = raw({"id": "1", "decision": "KEEP", "themes": [], "reason": ""})

    assert parse_verdicts(text, ITEMS[:1])[0].decision is Decision.KEEP


class FakeClassifier:
    def __init__(self) -> None:
        self.batches: list[list[int]] = []

    def classify(self, items):
        self.batches.append([item.id for item in items])
        return [HeadlineVerdict(item.id, Decision.KEEP, (), "") for item in items]


def test_classify_all_divide_em_lotes_e_preserva_a_ordem():
    items = [HeadlineItem(id=n, domain="a.com", text=f"manchete {n}") for n in range(1, 8)]
    fake = FakeClassifier()

    verdicts = classify_all(fake, items, batch_size=3, workers=1)

    assert fake.batches == [[1, 2, 3], [4, 5, 6], [7]]
    assert [v.id for v in verdicts] == [1, 2, 3, 4, 5, 6, 7]


def test_classify_all_em_paralelo_continua_ordenado():
    items = [HeadlineItem(id=n, domain="a.com", text=f"manchete {n}") for n in range(1, 41)]

    verdicts = classify_all(FakeClassifier(), items, batch_size=5, workers=4)

    assert [v.id for v in verdicts] == list(range(1, 41))


def test_classify_all_lote_que_explode_vira_desconhecido_sem_derrubar_os_outros():
    class Flaky(FakeClassifier):
        def classify(self, items):
            if items[0].id == 4:
                raise RuntimeError("boom")
            return super().classify(items)

    items = [HeadlineItem(id=n, domain="a.com", text=f"m {n}") for n in range(1, 7)]

    verdicts = classify_all(Flaky(), items, batch_size=3, workers=1)

    assert [v.decision for v in verdicts[:3]] == [Decision.KEEP] * 3
    assert [v.decision for v in verdicts[3:]] == [Decision.UNKNOWN] * 3


def test_politicas_do_prompt_diferem_mas_mantem_os_temas_e_o_formato():
    from buscador_midias.triage.prompt import Policy, prompt_version, system_prompt

    balanced = system_prompt(Policy.BALANCED)
    permissive = system_prompt(Policy.PERMISSIVE)

    assert balanced == SYSTEM_PROMPT
    assert permissive != balanced
    for slug in THEME_SLUGS:
        assert slug in permissive
    assert '"keep"' in permissive
    assert '"items"' in permissive
    assert prompt_version(Policy.BALANCED) != prompt_version(Policy.PERMISSIVE)


def test_aceita_array_json_puro_na_raiz_alem_do_objeto_com_items():
    text = json.dumps(
        [
            {"id": 1, "decision": "keep", "themes": ["fiscal"], "reason": "fraude"},
            {"id": 2, "decision": "discard", "themes": [], "reason": "clima"},
        ]
    )

    verdicts = parse_verdicts(text, ITEMS)

    assert [v.decision for v in verdicts] == [Decision.KEEP, Decision.DISCARD]


def test_aceita_array_puro_dentro_de_bloco_de_codigo_e_com_texto_em_volta():
    body = json.dumps([{"id": 1, "decision": "keep", "themes": [], "reason": "x"}])

    fenced = parse_verdicts("```json\n" + body + "\n```", ITEMS[:1])
    chatty = parse_verdicts("Aqui está o resultado:\n" + body + "\nEspero ter ajudado.", ITEMS[:1])

    assert fenced[0].decision is Decision.KEEP
    assert chatty[0].decision is Decision.KEEP


def test_json_invalido_ou_de_outro_formato_continua_sem_levantar():
    for text in ("[1, 2", '{"items": "nao é lista"}', "null", "[]", '"texto"'):
        assert [v.decision for v in parse_verdicts(text, ITEMS)] == [Decision.UNKNOWN] * 2


def test_prompts_das_politicas_antigas_nao_mudaram():
    import hashlib

    from buscador_midias.triage.prompt import Policy, system_prompt

    def digest(policy: Policy) -> str:
        return hashlib.sha256(system_prompt(policy).encode()).hexdigest()[:16]

    assert digest(Policy.BALANCED) == "f69d3fb02c0dd9c7"
    assert digest(Policy.PERMISSIVE) == "a2e105fd7d36d963"


def test_politica_entities_descarta_so_o_que_nao_tem_quem_identificar():
    from buscador_midias.triage.prompt import Policy, prompt_version, system_prompt

    text = system_prompt(Policy.ENTITIES)
    lower = text.lower()

    assert "pessoa ou organização" in lower
    assert "previsão do tempo" in lower and "horóscopo" in lower and "loteria" in lower
    assert "na dúvida" in lower
    for slug in ("corrupcao", "criminal", "pldft"):  # os temas continuam sendo apontados
        assert slug in text
    # esporte e celebridade NÃO são motivo de descarte nesta política
    assert "só viram" not in lower
    assert "esporte" in lower and "celebridades" in lower
    assert '"decision":"keep"|"discard"' in text
    assert prompt_version(Policy.ENTITIES).endswith("-entities")


def test_a_triagem_usa_a_politica_entities_por_padrao():
    import inspect

    from buscador_midias.triage.openrouter import classifier_from_env
    from buscador_midias.triage.prompt import Policy

    default = inspect.signature(classifier_from_env).parameters["policy"].default

    assert default is Policy.ENTITIES


def test_entities_mantem_manchete_ilegivel_e_conta_sigla_como_organizacao():
    from buscador_midias.triage.prompt import Policy, prompt_version, system_prompt

    text = system_prompt(Policy.ENTITIES)
    lower = text.lower()

    # ilegível (só número na URL, truncada, enigmática): não dá para julgar, então "keep"
    assert "ilegível" in lower and "truncada" in lower
    assert "sem sentido" not in lower  # não é mais motivo de descarte
    # instituição, empresa, órgão e sigla contam como organização, também em economia e ciência
    assert "sigla" in lower and "conab" in lower
    # cada ajuste de critério precisa de versão nova, senão `--retriage-discarded` não reabre nada
    assert prompt_version(Policy.ENTITIES) != "headline-gate-2026-10-15.2-entities"
    assert prompt_version(Policy.PERMISSIVE) == "headline-gate-2026-10-15.2-permissive"
    assert prompt_version(Policy.BALANCED) == "headline-gate-2026-10-15.2-balanced"
