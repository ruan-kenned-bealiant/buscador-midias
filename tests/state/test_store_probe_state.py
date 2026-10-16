from datetime import UTC, datetime, timedelta

from buscador_midias.domain.strategy import (
    CandidateOrigin,
    CandidateReport,
    ProbeState,
    RoundHealth,
    StrategyKind,
    Verdict,
)
from buscador_midias.state.store import SiteRunRecord, StateStore

T0 = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)


def state(site: str = "a.com", **kw) -> ProbeState:
    base = {
        "site_key": site,
        "probed_at": T0,
        "found": True,
        "streak": 0,
        "cooldown_until": T0,
        "refresh_at": T0 + timedelta(days=7),
    }
    base.update(kw)
    return ProbeState(**base)


def site_run(site: str, status: str = "ok", *, new: int = 0, seen: int = 0, skipped: int = 0):
    return SiteRunRecord(site, status, new, 0, 0, seen, 1, skipped, "")


def test_grava_e_le_o_estado_de_sondagem_e_a_regravacao_substitui():
    store = StateStore()

    store.record_probe_state(state(streak=1), "failing")
    store.record_probe_state(state("b.com", found=False), "")
    store.record_probe_state(state(streak=2, probed_at=T0 + timedelta(hours=8)), "silent")

    states = store.probe_states()
    assert set(states) == {"a.com", "b.com"}
    assert states["a.com"].streak == 2 and states["a.com"].probed_at == T0 + timedelta(hours=8)
    assert states["b.com"].found is False
    assert states["a.com"] == state(streak=2, probed_at=T0 + timedelta(hours=8))


def test_rodadas_recentes_vem_da_mais_nova_para_a_mais_antiga_com_o_inicio_da_rodada():
    store = StateStore()
    for n in range(4):
        run = store.start_run(T0 + timedelta(hours=8 * n))
        store.record_site_run(run, site_run("a.com", new=n, seen=100 + n, skipped=n))
        store.record_site_run(run, site_run("b.com", "error"))

    rounds = store.recent_rounds(per_site=3)

    assert [r.new_urls for r in rounds["a.com"]] == [3, 2, 1]
    assert rounds["a.com"][0] == RoundHealth(T0 + timedelta(hours=24), "ok", 3, 103, 3)
    assert [r.status for r in rounds["b.com"]] == ["error"] * 3


def test_rodadas_recentes_de_fonte_sem_rodada_nao_aparecem():
    assert StateStore().recent_rounds(per_site=5) == {}


def test_instante_da_sondagem_de_cada_fonte_com_estrategia():
    store = StateStore()
    report = CandidateReport(
        url="https://a.com/n",
        origin=CandidateOrigin.ROBOTS,
        verdict=Verdict.FRESH,
        kind=StrategyKind.FEED,
        measure=None,
        score=1,
        detail="",
    )
    store.replace_site_strategies("a.com", [report], T0)
    store.replace_site_strategies("b.com", [report], T0 + timedelta(hours=1))

    assert store.strategy_probe_times() == {"a.com": T0, "b.com": T0 + timedelta(hours=1)}
