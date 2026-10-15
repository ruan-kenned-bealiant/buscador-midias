import pytest

from buscador_midias.labeling.verdicts import NO, UNCERTAIN, UNLABELED, YES, parse_verdict


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sim", YES),
        (" Sim ", YES),
        ("ENTRA", YES),
        ("1", YES),
        ("nao", NO),
        ("NÃO", NO),
        ("não entra", NO),
        ("incerto", UNCERTAIN),
        ("talvez", UNCERTAIN),
        ("", UNLABELED),
        ("qualquer coisa", UNLABELED),
    ],
)
def test_sinonimos_do_vocabulario_novo(raw: str, expected: str):
    assert parse_verdict(raw) == expected


@pytest.mark.parametrize("old", ["adverso", "nao_adverso", "nao adverso", "ADVERSO"])
def test_palavras_do_vocabulario_antigo_nao_viram_sim_nem_nao(old: str):
    # "adverso" respondia a outra pergunta ("é negativa?"); lida como sim/não trocaria o sentido
    # do rótulo em silêncio. Fica "sem rótulo" e a pessoa percebe na tela.
    assert parse_verdict(old) == UNLABELED
