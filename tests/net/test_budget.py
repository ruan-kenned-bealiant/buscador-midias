from tests.strategy.helpers import HOME, FakeFetcher

from buscador_midias.net.budget import BudgetFetcher


def test_compartilha_saldo_e_nao_repete_http_apos_limite():
    f = FakeFetcher()
    elapsed = [0]
    b = BudgetFetcher(f, max_requests=2, seconds=10, allowed=lambda u: u.startswith(HOME),
                      clock=lambda: elapsed[0])
    b.get(HOME)
    b.get(HOME + '/feed')
    assert not b.get(HOME + '/mais').ok
    assert b.requests == len(f.calls) == 2 and b.exhausted


def test_prazo_e_exclusao_impedem_nova_chamada():
    f = FakeFetcher()
    elapsed = [0]
    b = BudgetFetcher(f, max_requests=20, seconds=10, allowed=lambda u: u == HOME,
                      clock=lambda: elapsed[0])
    assert not b.get(HOME + '/excluido').ok
    assert not f.calls
    b.get(HOME)
    elapsed[0] = 10
    assert not b.get(HOME).ok
    assert f.calls == [HOME] and b.exhausted
