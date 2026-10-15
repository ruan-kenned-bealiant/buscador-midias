import json

import pytest

from buscador_midias.triage.l0 import FilterReason
from buscador_midias.triage.l1 import (
    L1Filter,
    NaiveBayesHeadlineModel,
    threshold_for_recall,
    train_l1,
)

POSITIVES = [
    "pf prende empresario por fraude e lavagem de dinheiro",
    "operacao investiga corrupcao e desvio de verba publica",
    "mp denuncia prefeito por fraude em licitacao",
    "justica condena socio por sonegacao e lavagem",
] * 5
NEGATIVES = [
    "previsao do tempo hoje sol com nuvens em recife",
    "prefeitura abre inscricoes para curso de culinaria",
    "show de rock acontece neste sabado no parque",
    "time vence classico e sobe na tabela do campeonato",
] * 5


def trained(**kwargs) -> NaiveBayesHeadlineModel:
    return NaiveBayesHeadlineModel.train(POSITIVES, NEGATIVES, min_count=2, **kwargs)


def test_titulo_adverso_pontua_acima_do_neutro():
    model = trained()

    assert model.score("pf investiga fraude em licitacao").log_odds > 0
    assert model.score("previsao do tempo para o show do sabado").log_odds < 0


def test_palavras_desconhecidas_nao_dao_evidencia():
    score = trained().score("xyzzy plugh quux")

    assert score.known == 0
    assert score.log_odds == 0.0


def test_prioris_sao_equilibradas_e_classe_maior_nao_empurra_o_sinal():
    many_positives = NaiveBayesHeadlineModel.train(POSITIVES * 20, NEGATIVES, min_count=2)

    assert many_positives.score("previsao do tempo hoje com nuvens").log_odds < 0


def test_palavra_rara_fica_fora_do_vocabulario():
    model = NaiveBayesHeadlineModel.train(
        ["fraude grave", "fraude grande"], ["tempo bom"], min_count=2
    )

    assert model.score("grave").known == 0
    assert model.score("fraude").known == 1


def test_modelo_sobrevive_a_ida_e_volta_para_dict():
    model = trained()
    clone = NaiveBayesHeadlineModel.from_dict(json.loads(json.dumps(model.to_dict())))

    text = "pf prende empresario por fraude"
    assert clone.score(text) == model.score(text)


def test_limiar_mantem_pelo_menos_o_recall_pedido():
    scores = [float(n) for n in range(1, 101)]

    threshold = threshold_for_recall(scores, 0.9)

    assert sum(1 for s in scores if s >= threshold) >= 90
    assert sum(1 for s in scores if s >= threshold) <= 91


def test_limiar_de_lista_vazia_nao_descarta_nada():
    assert threshold_for_recall([], 0.98) == float("-inf")


def make_filter(model: NaiveBayesHeadlineModel | None = None, threshold: float = 0.0) -> L1Filter:
    return L1Filter(model or trained(), threshold=threshold)


def test_descarta_manchete_de_baixo_sinal_em_site_de_lingua_coberta():
    reason = make_filter().check(
        language="Português",
        url="https://a.com/x",
        title="Previsão do tempo hoje com sol e nuvens em Recife",
    )

    assert reason is FilterReason.LOW_SIGNAL


def test_mantem_manchete_adversa():
    reason = make_filter().check(
        language="Português",
        url="https://a.com/x",
        title="PF prende empresário por fraude e lavagem",
    )

    assert reason is None


def test_lingua_nao_coberta_passa_sem_ser_avaliada():
    reason = make_filter().check(
        language="Inglês",
        url="https://a.com/x",
        title="Previsão do tempo hoje com sol e nuvens em Recife",
    )

    assert reason is None


def test_manchete_curta_ou_sem_vocabulario_conhecido_passa():
    flt = make_filter()

    assert flt.check(language="Português", url="https://a.com/x", title="Tempo hoje") is None
    assert (
        flt.check(language="Português", url="https://a.com/x", title="xyzzy plugh quux wibble")
        is None
    )


def test_sem_titulo_usa_o_slug_da_url():
    reason = make_filter().check(
        language="Português",
        url="https://a.com/cotidiano/previsao-do-tempo-hoje-com-sol-e-nuvens-em-recife-123.html",
        title=None,
    )

    assert reason is FilterReason.LOW_SIGNAL


def test_filtro_persiste_em_arquivo(tmp_path):
    flt = L1Filter(
        trained(),
        threshold=-1.5,
        min_words=5,
        min_known=3,
        languages=frozenset({"Português", "Espanhol"}),
    )
    path = tmp_path / "l1.json"

    flt.save(path)
    loaded = L1Filter.load(path)

    assert loaded.threshold == -1.5
    assert loaded.min_words == 5
    assert loaded.min_known == 3
    assert loaded.languages == frozenset({"Português", "Espanhol"})
    text = "pf prende empresario por fraude"
    assert loaded.model.score(text) == flt.model.score(text)


def test_treino_calibra_o_limiar_pelo_recall_em_dados_separados():
    positives = [f"pf prende suspeito de fraude {n}" for n in range(200)]
    negatives = [f"previsao do tempo hoje {n}" for n in range(200)]

    flt, evaluation = train_l1(positives, negatives, recall=0.98, seed=1)

    assert evaluation.positive_recall >= 0.98
    assert evaluation.negative_pass_rate < 0.1
    assert evaluation.test_positives == 40
    assert evaluation.test_negatives == 40
    assert flt.threshold == evaluation.threshold


def test_treino_e_deterministico_para_a_mesma_semente():
    positives = [f"pf prende suspeito de fraude {n}" for n in range(100)] + [
        "corrupcao em obra"
    ] * 20
    negatives = [f"previsao do tempo hoje {n}" for n in range(100)] + ["show no parque"] * 20

    first = train_l1(positives, negatives, seed=7)[1]
    second = train_l1(positives, negatives, seed=7)[1]

    assert first == second


def test_treino_sem_exemplos_de_uma_classe_falha_com_mensagem_clara():
    with pytest.raises(ValueError, match="positivos"):
        train_l1([], ["a b c d"], seed=1)
    with pytest.raises(ValueError, match="negativos"):
        train_l1(["a b c d"], [], seed=1)
