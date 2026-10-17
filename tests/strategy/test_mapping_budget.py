import pytest
from tests.strategy.helpers import FakeFetcher

from buscador_midias.strategy.mapping_budget import MappingBudget, MappingBudgetExceededError


def test_limite_total_nao_repassa_chamada_excedente():
    inner = FakeFetcher()
    budget = MappingBudget(inner, max_requests=2, max_seconds=20, deadline=100, clock=lambda: 0)
    budget.get("https://exemplo.com/robots.txt")
    budget.get("https://exemplo.com/feed.xml")
    with pytest.raises(MappingBudgetExceededError, match="requisições"):
        budget.get("https://exemplo.com/politica")
    assert len(inner.calls) == budget.requests == 2


@pytest.mark.parametrize("site_seconds,global_deadline", [(3, 100), (100, 3)])
def test_prazo_da_fonte_e_global_impedem_nova_chamada(site_seconds, global_deadline):
    elapsed = [0]
    inner = FakeFetcher()
    budget = MappingBudget(inner, max_requests=20, max_seconds=site_seconds,
                           deadline=global_deadline, clock=lambda: elapsed[0])
    budget.get("https://exemplo.com")
    elapsed[0] = 3
    with pytest.raises(MappingBudgetExceededError, match="tempo"):
        budget.get("https://exemplo.com/feed.xml")
    assert len(inner.calls) == 1
