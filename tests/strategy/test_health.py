"""Quando reavaliar a estratégia de uma fonte: saúde das rodadas, idade e espera crescente."""

from datetime import UTC, datetime, timedelta

from buscador_midias.domain.strategy import ProbeState, RoundHealth
from buscador_midias.strategy.health import (
    DueReason,
    HealthPolicy,
    adopt,
    backoff,
    due_reason,
    next_state,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
POLICY = HealthPolicy()
PROBED = NOW - timedelta(days=2)


def state(**kw) -> ProbeState:
    base = {
        "site_key": "a.com",
        "probed_at": PROBED,
        "found": True,
        "streak": 0,
        "cooldown_until": PROBED,
        "refresh_at": PROBED + timedelta(days=7),
    }
    base.update(kw)
    return ProbeState(**base)


def rnd(hours_ago: float, *, status="ok", new=10, seen=100, skipped=0) -> RoundHealth:
    return RoundHealth(NOW - timedelta(hours=hours_ago), status, new, seen, skipped)


def due(rounds, st=None, **kw):
    return due_reason(st if st is not None else state(), rounds, now=NOW, policy=POLICY, **kw)


# --- sem estado, atraso e espera ---


def test_fonte_nunca_sondada_esta_na_vez():
    assert due_reason(None, [], now=NOW, policy=POLICY) is DueReason.NEVER_PROBED


def test_fonte_com_estrategia_em_dia_e_rodadas_saudaveis_nao_esta_na_vez():
    assert due([rnd(8), rnd(16), rnd(24)]) is None


def test_estrategia_velha_e_reavaliada_mesmo_com_rodadas_saudaveis():
    old = state(refresh_at=NOW - timedelta(minutes=1))

    assert due([rnd(8)], old) is DueReason.REFRESH


def test_fonte_sem_estrategia_so_tenta_de_novo_depois_da_espera():
    waiting = state(found=False, streak=1, refresh_at=NOW + timedelta(hours=3))
    ready = state(found=False, streak=1, refresh_at=NOW - timedelta(minutes=1))

    assert due([], waiting) is None
    assert due([], ready) is DueReason.RETRY


# --- falha, silêncio e encolhimento ---


def test_duas_rodadas_seguidas_com_falha_desde_a_sondagem_pedem_reavaliacao():
    assert due([rnd(8, status="error"), rnd(16, status="blocked"), rnd(30)]) is DueReason.FAILING


def test_uma_falha_so_nao_basta_e_falhas_antes_da_sondagem_nao_contam():
    assert due([rnd(8, status="error"), rnd(16)]) is None
    before = PROBED - timedelta(hours=1)
    old_failures = [
        rnd(8),
        RoundHealth(before, "error", 0, 0, 0),
        RoundHealth(before - timedelta(hours=8), "error", 0, 0, 0),
    ]
    assert due(old_failures) is None


def test_quatro_rodadas_ok_sem_nada_novo_numa_fonte_que_rendia_e_silencio():
    silent = [rnd(h, new=0) for h in (4, 12, 20, 28)]
    history = [rnd(h, new=15) for h in (52, 60, 68)]

    assert due(silent + history) is DueReason.SILENT


def test_silencio_de_fonte_que_nunca_rendeu_nao_pede_reavaliacao():
    silent = [rnd(h, new=0) for h in (4, 12, 20, 28)]
    history = [rnd(h, new=1) for h in (52, 60, 68)]  # só 3 no total: fonte quieta mesmo

    assert due(silent + history) is None


def test_menos_de_quatro_rodadas_silenciosas_nao_bastam():
    rounds = [rnd(4, new=0), rnd(12, new=0), rnd(20, new=0), rnd(60, new=15), rnd(68, new=15)]

    assert due(rounds) is None


def test_volume_listado_que_despenca_em_relacao_ao_historico_e_encolhimento():
    rounds = [rnd(4, seen=10), rnd(12), rnd(20), rnd(28), rnd(36)]  # historico: seen=100

    assert due(rounds) is DueReason.SHRUNK


def test_volume_baixo_numa_rodada_que_pulou_sitemaps_nao_e_encolhimento():
    rounds = [rnd(4, seen=10, skipped=5), rnd(12), rnd(20), rnd(28), rnd(36)]

    assert due(rounds) is None


def test_encolhimento_exige_historico_e_volume_minimos():
    assert due([rnd(4, seen=1), rnd(12), rnd(20)]) is None  # só 2 rodadas anteriores
    tiny = [rnd(4, seen=0), rnd(12, seen=10), rnd(20, seen=10), rnd(28, seen=10)]
    assert due(tiny) is None  # mediana 10 < 20: fonte pequena


def test_dentro_da_espera_de_saude_nao_reavalia_mesmo_com_falha():
    cooling = state(streak=2, cooldown_until=NOW + timedelta(hours=5))

    assert due([rnd(8, status="error"), rnd(16, status="error")], cooling) is None


def test_a_idade_vale_mesmo_dentro_da_espera_de_saude():
    cooling = state(
        streak=2, cooldown_until=NOW + timedelta(hours=5), refresh_at=NOW - timedelta(hours=1)
    )

    assert due([rnd(8)], cooling) is DueReason.REFRESH


# --- espera crescente e novo estado ---


def test_espera_dobra_a_cada_tentativa_sem_melhora_ate_o_teto():
    assert [backoff(n, POLICY) for n in (1, 2, 3)] == [
        timedelta(hours=6),
        timedelta(hours=12),
        timedelta(hours=24),
    ]
    assert backoff(20, POLICY) == timedelta(days=7)


def test_sondagem_que_acha_estrategia_zera_a_espera_e_marca_a_idade():
    new = next_state(
        state(streak=3), "a.com", now=NOW, found=True, changed=True,
        reason=DueReason.FAILING, policy=POLICY,
    )  # fmt: skip

    assert new.found and new.streak == 0
    assert new.probed_at == NOW and new.refresh_at == NOW + timedelta(days=7)
    assert new.cooldown_until == NOW


def test_reavaliacao_por_saude_que_nao_muda_nada_aumenta_a_espera():
    new = next_state(
        state(streak=0), "a.com", now=NOW, found=True, changed=False,
        reason=DueReason.SILENT, policy=POLICY,
    )  # fmt: skip

    assert new.streak == 1 and new.cooldown_until == NOW + timedelta(hours=6)


def test_reavaliacao_por_idade_ou_manual_nunca_conta_como_tentativa_sem_melhora():
    for reason in (None, DueReason.REFRESH, DueReason.NEVER_PROBED, DueReason.RETRY):
        new = next_state(
            state(streak=2), "a.com", now=NOW, found=True, changed=False,
            reason=reason, policy=POLICY,
        )  # fmt: skip
        assert new.streak == 0, reason


def test_sondagem_sem_estrategia_cresce_a_espera_e_agenda_a_nova_tentativa():
    first = next_state(
        None, "a.com", now=NOW, found=False, changed=False, reason=None, policy=POLICY
    )
    second = next_state(
        first, "a.com", now=NOW, found=False, changed=False, reason=DueReason.RETRY, policy=POLICY
    )

    assert (first.found, first.streak, first.refresh_at) == (False, 1, NOW + timedelta(hours=6))
    assert (second.streak, second.refresh_at) == (2, NOW + timedelta(hours=12))


def test_estrategia_que_ja_existia_sem_estado_e_adotada_com_a_idade_da_sondagem():
    adopted = adopt("a.com", PROBED, POLICY)

    assert adopted.found and adopted.streak == 0
    assert adopted.probed_at == PROBED and adopted.refresh_at == PROBED + timedelta(days=7)


def test_rodadas_de_antes_da_sondagem_nao_servem_de_base_para_o_encolhimento():
    # Antes da sondagem a fonte era lida de outro jeito (ex.: a cascata lia sitemaps de arquivo):
    # o volume "listado" daquelas rodadas não é comparável com o da estratégia atual.
    before = PROBED - timedelta(hours=1)
    old = [RoundHealth(before - timedelta(hours=8 * n), "ok", 10, 5000, 0) for n in range(5)]

    assert due([rnd(4, seen=100), *old]) is None
