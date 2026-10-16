"""Medição, veredito, nota e escolha das estratégias de descoberta de uma fonte."""

from datetime import UTC, datetime, timedelta

from buscador_midias.domain.strategy import (
    CandidateOrigin,
    CandidateReport,
    Item,
    Measure,
    Role,
    StrategyKind,
    Verdict,
)
from buscador_midias.strategy.measure import measure_items
from buscador_midias.strategy.scoring import ScoringPolicy, choose, judge, score

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def item(n: int, *, hours_ago: float | None = 1, title: str | None = "Título") -> Item:
    at = None if hours_ago is None else NOW - timedelta(hours=hours_ago)
    return Item(f"https://exemplo.com/materia-{n}", at, title)


def measure(kind=StrategyKind.NEWS_SITEMAP, items=(), **extra) -> Measure:
    return measure_items(kind, list(items), now=NOW, size_bytes=1000, **extra)


# --- medição ---


def test_conta_itens_datados_recentes_e_com_titulo():
    result = measure(
        items=[
            item(1, hours_ago=2),
            item(2, hours_ago=47),
            item(3, hours_ago=200),
            item(4, hours_ago=None, title=None),
        ]
    )

    assert (result.items, result.dated, result.recent, result.titled) == (4, 3, 2, 3)
    assert result.newest == NOW - timedelta(hours=2)
    assert result.size_bytes == 1000


def test_data_um_pouco_no_futuro_ainda_e_recente_mas_muito_no_futuro_nao():
    # Sites publicam em horário local sem fuso: até algumas horas à frente é erro de fuso.
    result = measure(items=[item(1, hours_ago=-5), item(2, hours_ago=-72)])

    assert result.recent == 1


def test_sem_itens_a_medida_e_vazia():
    result = measure(items=[])

    assert (result.items, result.dated, result.recent, result.newest) == (0, 0, 0, None)


# --- veredito ---


def test_fonte_com_itens_recentes_e_fresca():
    assert judge(measure(items=[item(1), item(2)])) is Verdict.FRESH


def test_sem_itens_e_vazia():
    assert judge(measure(items=[])) is Verdict.EMPTY


def test_itens_sem_nenhuma_data_nao_provam_frescor():
    assert judge(measure(items=[item(1, hours_ago=None)])) is Verdict.UNDATED


def test_itens_so_antigos_estao_defasados():
    assert judge(measure(items=[item(1, hours_ago=500)])) is Verdict.STALE


def test_lista_enorme_quase_sem_itens_recentes_e_arquivo():
    policy = ScoringPolicy(archive_min_items=100)
    items = [item(n, hours_ago=1000) for n in range(150)] + [item(999, hours_ago=1)]

    assert judge(measure(items=items), policy) is Verdict.ARCHIVE


def test_lista_enorme_sem_datas_tambem_e_arquivo():
    policy = ScoringPolicy(archive_min_items=100)

    assert judge(measure(items=[item(n, hours_ago=None) for n in range(150)]), policy) is (
        Verdict.ARCHIVE
    )


def test_lista_enorme_mas_quase_toda_recente_nao_e_arquivo():
    policy = ScoringPolicy(archive_min_items=100)

    assert judge(measure(items=[item(n, hours_ago=3) for n in range(150)]), policy) is (
        Verdict.FRESH
    )


# --- nota ---


def test_so_fresca_ou_sem_data_tem_nota():
    m = measure(items=[item(1)])

    for verdict in (Verdict.STALE, Verdict.ARCHIVE, Verdict.EMPTY, Verdict.BLOCKED):
        assert score(m, verdict) == 0.0
    assert score(m, Verdict.FRESH) > 0
    assert score(m, Verdict.UNDATED) > 0


def test_mais_itens_recentes_dao_nota_maior_ate_o_teto():
    policy = ScoringPolicy(coverage_cap=100)
    few = measure(items=[item(n) for n in range(10)])
    many = measure(items=[item(n) for n in range(100)])
    huge = measure(items=[item(n) for n in range(500)])

    assert score(few, Verdict.FRESH, policy) < score(many, Verdict.FRESH, policy)
    assert score(many, Verdict.FRESH, policy) == score(huge, Verdict.FRESH, policy)


def test_news_sitemap_vale_mais_que_sitemap_generico_com_a_mesma_cobertura():
    items = [item(n) for n in range(30)]

    news = score(measure(StrategyKind.NEWS_SITEMAP, items), Verdict.FRESH)
    feed = score(measure(StrategyKind.FEED, items), Verdict.FRESH)
    generic = score(measure(StrategyKind.SITEMAP, items), Verdict.FRESH)

    assert news > feed > generic


def test_fonte_sem_titulo_vale_menos():
    titled = measure(items=[item(n) for n in range(20)])
    untitled = measure(items=[item(n, title=None) for n in range(20)])

    assert score(untitled, Verdict.FRESH) < score(titled, Verdict.FRESH)


def test_sem_data_vale_menos_que_qualquer_fresca():
    undated = score(measure(items=[item(1, hours_ago=None)]), Verdict.UNDATED)
    fresh = score(measure(items=[item(1)]), Verdict.FRESH)

    assert undated < fresh


# --- escolha ---


def report(url: str, kind: StrategyKind | None, value: float, verdict=Verdict.FRESH):
    return CandidateReport(
        url=url,
        origin=CandidateOrigin.ROBOTS,
        verdict=verdict,
        kind=kind,
        measure=None,
        score=value,
        detail="",
    )


def test_escolhe_a_melhor_como_principal_e_a_melhor_de_outro_tipo_como_reserva():
    reports = [
        report("a", StrategyKind.SITEMAP, 5),
        report("b", StrategyKind.NEWS_SITEMAP, 80),
        report("c", StrategyKind.NEWS_SITEMAP, 70),
        report("d", StrategyKind.FEED, 40),
    ]

    chosen = {r.url: r.role for r in choose(reports)}

    assert chosen == {"a": None, "b": Role.PRIMARY, "c": None, "d": Role.BACKUP}


def test_sem_outro_tipo_nao_ha_reserva():
    reports = [report("a", StrategyKind.FEED, 10), report("b", StrategyKind.FEED, 9)]

    assert [r.role for r in choose(reports)] == [Role.PRIMARY, None]


def test_candidatas_sem_nota_nunca_sao_escolhidas():
    reports = [
        report("a", StrategyKind.FEED, 0, Verdict.STALE),
        report("b", None, 0, Verdict.BLOCKED),
    ]

    assert all(r.role is None for r in choose(reports))


def test_empate_de_nota_desempata_pela_url_para_ser_deterministico():
    reports = [report("b", StrategyKind.FEED, 10), report("a", StrategyKind.FEED, 10)]

    assert {r.url: r.role for r in choose(reports)}["a"] is Role.PRIMARY


def test_a_escolha_mantem_a_ordem_e_nao_altera_o_original():
    reports = [report("a", StrategyKind.FEED, 1), report("b", StrategyKind.NEWS_SITEMAP, 2)]

    chosen = choose(reports)

    assert [r.url for r in chosen] == ["a", "b"]
    assert all(r.role is None for r in reports)
