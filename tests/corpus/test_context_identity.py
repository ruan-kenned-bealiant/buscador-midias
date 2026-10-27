"""Contrato de identidade nominal do KyX e proteção contra nomes não latinos vazios."""

import hashlib

from buscador_midias.corpus.contexts import entity_identity


def test_hash_nominal_usa_mesma_sanitizacao_ascii_do_kyx():
    assert entity_identity("João Silva", "PF") == ("pf", hashlib.sha256(b"pf|joosilva").hexdigest())
    assert entity_identity("Empresa A/S", "PJ") == (
        "pj",
        hashlib.sha256(b"pj|empresaas").hexdigest(),
    )


def test_organizacao_publica_nao_vira_pf_e_nomes_nao_latinos_nao_colidem():
    assert entity_identity("Ministério Público", "OUTRO")[0] == "pj"
    assert entity_identity("张伟", "PF")[1] != entity_identity("王伟", "PF")[1]
    assert entity_identity("张伟", "PF")[1] != hashlib.sha256(b"pf|").hexdigest()
