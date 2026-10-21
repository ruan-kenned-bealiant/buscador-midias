import pytest

from buscador_midias.analysis.mechanism_metrics import exclusives, replay_coverage


def test_exclusividade_nao_confunde_ocorrencias_com_links_distintos():
    result = exclusives({'rss': {'a', 'b'}, 'sitemap': {'b', 'c'}, 'vazio': set()})
    assert result == {'rss': {'a'}, 'sitemap': {'c'}, 'vazio': set()}


def test_simulacao_reaproveita_documentos_e_pula_raiz_sem_ganho():
    candidates = {'a': {'1', '2'}, 'b': {'2', '3'}, 'redundante': {'1', '2'}}
    documents = {'a': {'xml1'}, 'b': {'xml1', 'xml2'}, 'redundante': {'caro1', 'caro2'}}
    result = replay_coverage(candidates, documents)
    assert [r.mechanism for r in result] == ['a', 'b']
    assert result[-1].covered_candidates == 3
    assert result[-1].read_documents == 2
    assert [r.added_documents for r in result] == [1, 1]


def test_custo_ausente_nao_vira_custo_zero():
    with pytest.raises(ValueError, match='custo documental'):
        replay_coverage({'mecanismo': {'noticia'}}, {})


def test_conjunto_vazio_nao_gera_simulacao_de_ganho():
    assert replay_coverage({'sem_novas': set()}, {}) == []
