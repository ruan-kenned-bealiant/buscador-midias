import json

import pytest
from tests.strategy.helpers import HOME, make_site

from buscador_midias.domain.strategy import Role
from buscador_midias.strategy.discovery_plan import load_discovery_plan


def test_carrega_plano_do_piloto_com_principal_e_complementos(tmp_path):
    p = tmp_path / "plano.json"
    p.write_text(json.dumps([{'chave': 'exemplo.com', 'mecanismos': [
        {'url': HOME + '/news.xml', 'kind': 'news_sitemap'},
        {'url': HOME + '/feed', 'kind': 'feed'},
    ]}]))
    loaded = load_discovery_plan(p, {'exemplo.com': make_site()})
    assert [r.role for r in loaded['exemplo.com']] == [Role.PRIMARY, Role.COMPLEMENT]


@pytest.mark.parametrize('members', [
    [], [{'url': 'https://fora.com/feed', 'kind': 'feed'}],
    [{'url': HOME, 'kind': 'listing'}], [{'url': 23, 'kind': 'feed'}],
    [{'url': HOME + '/feed?signature=credencial', 'kind': 'feed'}],
    [{'url': HOME + '/feed', 'kind': 'feed'}, {'url': HOME + '/feed/', 'kind': 'feed'}],
])
def test_rejeita_plano_invalido_antes_de_rede(tmp_path, members):
    p = tmp_path / "plano.json"
    p.write_text(json.dumps([{'chave': 'exemplo.com', 'mecanismos': members}]))
    with pytest.raises(ValueError, match='plano de descoberta inválido'):
        load_discovery_plan(p, {'exemplo.com': make_site()})
