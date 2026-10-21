from buscador_midias.analysis.mechanism_selection import MechanismEvidence, classify


def test_sem_novidades_nao_exclui_xml_funcional():
    assert classify(MechanismEvidence(True, xml_confirmed=True))[0] == "periodico"


def test_audio_isolado_em_sitemap_geral_nao_exclui_mecanismo():
    assert classify(MechanismEvidence(True, sampled=1, sampled_audio=1))[0] == "inconclusivo"


def test_xml_podcast_comprovado_e_amostra_audio_autorizam_exclusao_da_selecao():
    evidence = MechanismEvidence(True, sampled=3, sampled_audio=3, dedicated_audio_proven=True)
    assert classify(evidence)[0] == "excluir"


def test_texto_confirmado_em_mecanismo_misto_prevalece_sobre_sinal_audio():
    evidence = MechanismEvidence(
        True, useful_texts=1, sampled=3, sampled_audio=2, dedicated_audio_proven=True
    )
    assert classify(evidence)[0] == "frequente"


def test_origem_sintetica_nao_vira_endpoint_por_ter_texto():
    assert classify(MechanismEvidence(False, useful_texts=5))[0] == "inconclusivo"


def test_custo_desconhecido_nao_e_reprovacao_nem_zero_de_custo():
    assert classify(MechanismEvidence(True, useful_texts=1, documents=None))[0] == "frequente"
    assert classify(MechanismEvidence(True, useful_texts=1, documents=40000))[0] == "periodico"


def test_percurso_parcial_permanece_explicito():
    assert classify(MechanismEvidence(True, useful_texts=1, pending=5))[0] == "periodico"
