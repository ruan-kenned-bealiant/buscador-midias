from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from buscador_midias.domain.strategy import (
    CandidateOrigin,
    CandidateReport,
    Measure,
    Role,
    StrategyKind,
    Verdict,
)
from buscador_midias.state.store import StateStore

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def report(
    url: str,
    *,
    kind: StrategyKind | None = StrategyKind.NEWS_SITEMAP,
    verdict: Verdict = Verdict.FRESH,
    role: Role | None = None,
    value: float = 10.0,
    with_measure: bool = True,
) -> CandidateReport:
    measure = (
        Measure(kind, items=20, dated=18, recent=15, titled=20, newest=T0, size_bytes=4096)
        if with_measure and kind is not None
        else None
    )
    return CandidateReport(
        url=url,
        origin=CandidateOrigin.ROBOTS,
        verdict=verdict,
        kind=kind,
        measure=measure,
        score=value,
        detail="ok",
        requests=2,
        role=role,
    )


def test_grava_os_candidatos_e_devolve_com_a_medida_e_o_papel():
    store = StateStore()
    reports = [
        report("https://a.com/news.xml", role=Role.PRIMARY, value=15),
        report("https://a.com/feed", kind=StrategyKind.FEED, role=Role.BACKUP, value=9),
        report(
            "https://a.com/velho.xml",
            kind=None,
            verdict=Verdict.UNAVAILABLE,
            value=0,
            with_measure=False,
        ),
    ]

    store.replace_site_strategies("a.com", reports, T0)
    saved = {r.url: r for r in store.site_strategies("a.com")}

    assert set(saved) == {r.url for r in reports}
    primary = saved["https://a.com/news.xml"]
    assert primary.role is Role.PRIMARY and primary.kind is StrategyKind.NEWS_SITEMAP
    assert primary.measure == reports[0].measure
    assert (primary.origin, primary.verdict, primary.score) == (
        CandidateOrigin.ROBOTS,
        Verdict.FRESH,
        15,
    )
    assert primary.requests == 2 and primary.detail == "ok"
    gone = saved["https://a.com/velho.xml"]
    assert gone.measure is None and gone.kind is None and gone.verdict is Verdict.UNAVAILABLE


def test_a_nova_sondagem_substitui_as_linhas_da_fonte_e_so_dela():
    store = StateStore()
    store.replace_site_strategies("a.com", [report("https://a.com/velho", role=Role.PRIMARY)], T0)
    store.replace_site_strategies("b.com", [report("https://b.com/x", role=Role.PRIMARY)], T0)

    store.replace_site_strategies("a.com", [report("https://a.com/novo", role=Role.PRIMARY)], T0)

    assert [r.url for r in store.site_strategies("a.com")] == ["https://a.com/novo"]
    assert [r.url for r in store.site_strategies("b.com")] == ["https://b.com/x"]


def test_sondagem_sem_candidatos_limpa_a_fonte():
    store = StateStore()
    store.replace_site_strategies("a.com", [report("https://a.com/x")], T0)

    store.replace_site_strategies("a.com", [], T0 + timedelta(hours=1))

    assert store.site_strategies("a.com") == []


def test_guarda_o_instante_da_sondagem():
    store = StateStore()
    store.replace_site_strategies("a.com", [report("https://a.com/x")], T0)

    assert store.strategy_probed_at("a.com") == T0
    assert store.strategy_probed_at("outra.com") is None


def test_chosen_lista_so_principal_e_reserva_de_todas_as_fontes():
    store = StateStore()
    store.replace_site_strategies(
        "a.com",
        [report("https://a.com/n", role=Role.PRIMARY), report("https://a.com/o")],
        T0,
    )
    store.replace_site_strategies(
        "b.com", [report("https://b.com/f", kind=StrategyKind.FEED, role=Role.PRIMARY)], T0
    )

    chosen = store.chosen_strategies()

    assert {(site, r.url, r.role) for site, r in chosen} == {
        ("a.com", "https://a.com/n", Role.PRIMARY),
        ("b.com", "https://b.com/f", Role.PRIMARY),
    }


def test_o_banco_nao_aceita_dois_principais_na_mesma_fonte():
    store = StateStore()
    reports = [
        report("https://a.com/1", role=Role.PRIMARY),
        report("https://a.com/2", role=Role.PRIMARY),
    ]

    with pytest.raises(psycopg.errors.UniqueViolation):
        store.replace_site_strategies("a.com", reports, T0)

    assert store.site_strategies("a.com") == []  # a transação desfez a troca


def test_chosen_traz_o_principal_antes_da_reserva_de_cada_fonte():
    store = StateStore()
    store.replace_site_strategies(
        "a.com",
        [
            report("https://a.com/feed", kind=StrategyKind.FEED, role=Role.BACKUP),
            report("https://a.com/n", role=Role.PRIMARY),
        ],
        T0,
    )

    assert [r.role for _, r in store.chosen_strategies()] == [Role.PRIMARY, Role.BACKUP]


def test_grava_e_le_secoes_cobertura_e_complemento():
    store = StateStore()
    measure = Measure(
        StrategyKind.FEED,
        items=50,
        dated=50,
        recent=40,
        titled=50,
        newest=T0,
        size_bytes=100,
        sections=3,
        top_sections=(("politica", 20), ("mundo", 15), ("sp", 5)),
    )
    complement = CandidateReport(
        url="https://a.com/feed",
        origin=CandidateOrigin.HOME_LINK,
        verdict=Verdict.FRESH,
        kind=StrategyKind.FEED,
        measure=measure,
        score=30,
        detail="",
        requests=1,
        role=Role.COMPLEMENT,
        coverage=0.4,
        union_recent=100,
        cover_share=0.95,
    )

    store.replace_site_strategies(
        "a.com", [report("https://a.com/n", role=Role.PRIMARY), complement], T0
    )
    saved = {r.url: r for r in store.site_strategies("a.com")}["https://a.com/feed"]

    assert saved.role is Role.COMPLEMENT
    assert saved.measure == measure
    assert (saved.coverage, saved.union_recent, saved.cover_share) == (0.4, 100, 0.95)


def test_o_que_a_descoberta_usa_e_so_o_principal_e_a_reserva():
    store = StateStore()
    store.replace_site_strategies(
        "a.com",
        [
            report("https://a.com/n", role=Role.PRIMARY),
            report("https://a.com/b", kind=StrategyKind.FEED, role=Role.BACKUP),
            report("https://a.com/c", kind=StrategyKind.SITEMAP, role=Role.COMPLEMENT),
        ],
        T0,
    )

    assert [r.url for _, r in store.chosen_strategies()] == ["https://a.com/n", "https://a.com/b"]
    assert [r.url for _, r in store.discovery_strategies()] == [
        "https://a.com/n", "https://a.com/b", "https://a.com/c",
    ]


def test_strategy_rows_traz_todos_os_candidatos_com_a_fonte_e_o_instante():
    store = StateStore()
    store.replace_site_strategies(
        "a.com", [report("https://a.com/n", role=Role.PRIMARY), report("https://a.com/o")], T0
    )
    store.replace_site_strategies("b.com", [report("https://b.com/x")], T0 + timedelta(hours=1))

    rows = store.strategy_rows()

    assert {(site, r.url, at) for site, r, at in rows} == {
        ("a.com", "https://a.com/n", T0),
        ("a.com", "https://a.com/o", T0),
        ("b.com", "https://b.com/x", T0 + timedelta(hours=1)),
    }


def test_grava_e_devolve_uma_estrategia_lenta():
    store = StateStore()
    slow = report(
        "https://a.com/feed", kind=StrategyKind.FEED, verdict=Verdict.SLOW, role=Role.PRIMARY
    )

    store.replace_site_strategies("a.com", [slow], T0)

    assert store.site_strategies("a.com")[0].verdict is Verdict.SLOW
