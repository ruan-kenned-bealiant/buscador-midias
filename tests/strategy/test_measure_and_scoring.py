"""Medição, veredito, nota e escolha das estratégias de descoberta de uma fonte."""

from dataclasses import replace
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


def test_fonte_que_publica_devagar_e_lenta_nao_defasada():
    """A janela de 48 h é só uma régua da POC: quem publicou no último trimestre está viva."""
    ten_days = measure(items=[item(1, hours_ago=240)])

    assert judge(ten_days, now=NOW) is Verdict.SLOW


def test_fonte_sem_nada_no_ultimo_trimestre_continua_defasada():
    old = measure(items=[item(1, hours_ago=24 * 200)])

    assert judge(old, now=NOW) is Verdict.STALE


def test_sem_o_instante_de_referencia_nao_se_conclui_lenta():
    assert judge(measure(items=[item(1, hours_ago=240)])) is Verdict.STALE


def test_a_janela_da_fonte_lenta_e_ajustavel():
    policy = ScoringPolicy(slow_window_days=5)

    assert judge(measure(items=[item(1, hours_ago=240)]), policy, now=NOW) is Verdict.STALE


def test_um_item_com_data_no_futuro_distante_nao_vira_lenta():
    future = measure(items=[item(1, hours_ago=-24 * 30)])

    assert judge(future, now=NOW) is Verdict.STALE


def test_itens_so_antigos_estao_defasados():
    assert judge(measure(items=[item(1, hours_ago=500)])) is Verdict.STALE


def test_lista_enorme_quase_sem_itens_recentes_e_arquivo():
    policy = ScoringPolicy(archive_min_items=100)
    items = [item(n, hours_ago=1000) for n in range(150)] + [item(999, hours_ago=1)]

    assert judge(measure(items=items), policy) is Verdict.ARCHIVE


def test_lista_enorme_com_muitos_recentes_em_numero_absoluto_nao_e_arquivo():
    """Um sitemap de 10 mil URLs com centenas de recentes é vivo, mesmo com poucos por cento."""
    policy = ScoringPolicy(archive_min_items=100, archive_keep_recent=10)
    items = [item(n, hours_ago=1000) for n in range(300)]
    items += [item(1000 + n, hours_ago=1) for n in range(12)]  # 3,8% < 5%, mas são 12

    assert judge(measure(items=items), policy) is Verdict.FRESH


def test_lista_enorme_com_poucos_recentes_continua_sendo_arquivo():
    policy = ScoringPolicy(archive_min_items=100, archive_keep_recent=10)
    items = [item(n, hours_ago=1000) for n in range(300)]
    items += [item(1000 + n, hours_ago=1) for n in range(5)]

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


def test_lenta_tem_nota_menor_que_qualquer_fonte_fresca_e_maior_que_sem_data():
    one_recent = score(measure(items=[item(1)]), Verdict.FRESH)
    slow = score(measure(items=[item(1, hours_ago=240)]), Verdict.SLOW)
    undated = score(measure(items=[item(1, hours_ago=None)]), Verdict.UNDATED)

    assert undated < slow < one_recent


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


def test_endereco_forcado_a_mao_vence_a_nota_se_serve():
    reports = [
        report("a", StrategyKind.NEWS_SITEMAP, 90),
        replace(report("b", StrategyKind.FEED, 5), forced=True),
    ]

    chosen = {r.url: r.role for r in choose(reports)}

    assert chosen == {"a": Role.BACKUP, "b": Role.PRIMARY}


def test_endereco_forcado_que_nao_serve_nao_e_escolhido():
    reports = [
        report("a", StrategyKind.NEWS_SITEMAP, 90),
        replace(report("b", StrategyKind.FEED, 0, Verdict.STALE), forced=True),
    ]

    assert {r.url: r.role for r in choose(reports)}["a"] is Role.PRIMARY


def test_a_lenta_e_escolhida_quando_nao_ha_nada_melhor_e_perde_para_a_fresca():
    slow = report("lenta", StrategyKind.FEED, 0.7, Verdict.SLOW)
    fresh = report("fresca", StrategyKind.SITEMAP, 5)

    assert {r.url: r.role for r in choose([slow])}["lenta"] is Role.PRIMARY
    assert {r.url: r.role for r in choose([slow, fresh])}["fresca"] is Role.PRIMARY


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


# --- seções (a fonte cobre o site todo ou só uma parte?) ---


def test_secao_e_o_primeiro_segmento_do_caminho_depois_do_prefixo_da_fonte():
    from buscador_midias.strategy.measure import section_of

    assert section_of("https://g1.globo.com/sp/ribeirao-preto/noticia/2026/10/16/x.ghtml") == "sp"
    assert section_of("https://g1.globo.com/globonews/a/b", "/globonews") == "a"
    assert section_of("https://x.com/Politica/y") == "politica"


def test_url_sem_secao_legivel_vai_para_outros():
    from buscador_midias.strategy.measure import OTHER_SECTION, section_of

    assert section_of("https://x.com/") == OTHER_SECTION
    assert section_of("https://x.com/2026/10/16/materia") == OTHER_SECTION  # data, não seção
    assert section_of("https://x.com/materia-1.html") == OTHER_SECTION
    assert section_of("https://x.com/12345") == OTHER_SECTION


def test_um_unico_segmento_e_o_titulo_da_materia_nao_uma_secao():
    """URL plana (/titulo-da-materia): sem isso, cada matéria virava uma "seção"."""
    from buscador_midias.strategy.measure import OTHER_SECTION, section_of

    assert section_of("https://x.com/governo-anuncia-medida-para-o-setor") == OTHER_SECTION
    assert section_of("https://x.com/governo-anuncia-medida-para-o-setor/") == OTHER_SECTION
    assert section_of("https://x.com/politica/governo-anuncia-medida") == "politica"


def test_a_medida_conta_secoes_so_entre_os_itens_recentes():
    items = [
        Item("https://exemplo.com/a", NOW - timedelta(hours=1), "t", "politica"),
        Item("https://exemplo.com/b", NOW - timedelta(hours=2), "t", "politica"),
        Item("https://exemplo.com/c", NOW - timedelta(hours=3), "t", "esporte"),
        Item("https://exemplo.com/d", NOW - timedelta(hours=900), "t", "arquivo"),
    ]

    result = measure(items=items)

    assert result.sections == 2
    assert result.top_sections == (("politica", 2), ("esporte", 1))


def test_sem_itens_recentes_as_secoes_vem_de_todos_os_itens():
    items = [Item("https://exemplo.com/a", None, "t", "politica")]

    assert measure(items=items).top_sections == (("politica", 1),)


def test_so_as_secoes_mais_frequentes_ficam_na_medida():
    items = [Item(f"https://x.com/{n}", NOW, "t", f"s{n}") for n in range(60)]

    result = measure(items=items)

    assert result.sections == 60 and len(result.top_sections) == 40


# --- cobertura: a união das fontes é o "site todo" que dá para ver ---


def covered(url: str, urls: set[str], kind=StrategyKind.NEWS_SITEMAP, value: float = 10.0):
    return CandidateReport(
        url=url,
        origin=CandidateOrigin.ROBOTS,
        verdict=Verdict.FRESH,
        kind=kind,
        measure=None,
        score=value,
        detail="",
        recent_urls=frozenset(urls),
    )


def urls(*names: str) -> set[str]:
    return {f"https://x.com/{n}" for n in names}


def test_cobertura_de_cada_candidato_e_a_fracao_da_uniao_que_ele_traz():
    from buscador_midias.strategy.scoring import plan_coverage

    reports = [covered("a", urls("1", "2", "3", "4")), covered("b", urls("3", "4", "5", "6"))]

    planned = plan_coverage(choose(reports))

    assert [r.coverage for r in planned] == [4 / 6, 4 / 6]
    assert all(r.union_recent == 6 for r in planned)


def test_o_complemento_e_a_fonte_que_mais_acrescenta_ao_que_ja_esta_coberto():
    from buscador_midias.strategy.scoring import plan_coverage

    reports = [
        covered("principal", urls(*map(str, range(0, 60))), value=60),
        covered("repete", urls(*map(str, range(0, 55))), kind=StrategyKind.FEED, value=50),
        covered("acrescenta", urls(*map(str, range(55, 80))), kind=StrategyKind.SITEMAP, value=5),
    ]

    planned = {r.url: r for r in plan_coverage(choose(reports))}

    assert planned["principal"].role is Role.PRIMARY
    assert planned["repete"].role is Role.BACKUP
    assert planned["acrescenta"].role is Role.COMPLEMENT
    assert planned["acrescenta"].cover_share == 1.0


def test_fonte_que_nao_acrescenta_quase_nada_nao_vira_complemento():
    from buscador_midias.strategy.scoring import plan_coverage

    reports = [
        covered("principal", urls(*map(str, range(0, 100))), value=100),
        covered("quase-igual", urls(*map(str, range(0, 101))), value=9),
    ]

    planned = {r.url: r for r in plan_coverage(choose(reports))}

    assert planned["quase-igual"].role is None  # 1 URL a mais não vale outra leitura
    assert planned["principal"].cover_share == 100 / 101


def test_so_candidatos_frescos_entram_na_uniao_e_o_papel_ja_dado_e_mantido():
    from buscador_midias.strategy.scoring import plan_coverage

    stale = CandidateReport(
        url="velho",
        origin=CandidateOrigin.ROBOTS,
        verdict=Verdict.STALE,
        kind=StrategyKind.FEED,
        measure=None,
        score=0,
        detail="",
    )
    reports = [covered("a", urls("1", "2")), stale]

    planned = plan_coverage(choose(reports))

    assert planned[0].role is Role.PRIMARY and planned[0].coverage == 1.0
    assert planned[1].coverage is None and planned[1].role is None


def test_sem_nenhum_candidato_fresco_nada_muda():
    from buscador_midias.strategy.scoring import plan_coverage

    assert plan_coverage(()) == ()
