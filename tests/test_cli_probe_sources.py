from pathlib import Path

import pytest
from tests.strategy.helpers import (
    NOW,
    FakeFetcher,
    news_sitemap,
    plain_sitemap,
    robots,
    rss,
    status,
)

from buscador_midias.cli import build_parser, main
from buscador_midias.domain.strategy import Role, StrategyKind
from buscador_midias.state.store import StateStore

SITES = (
    "region,name,url,country,country_code,language,source_type\n"
    "Brasil,A,https://a.com,Brasil,BR,Português,Jornal\n"
    "Brasil,B,https://b.com,Brasil,BR,Português,Jornal\n"
)
NEWS_A = "https://a.com/news.xml"


def world() -> FakeFetcher:
    """A tem sitemap de notícias e feed; B bloqueia o robots.txt."""
    return FakeFetcher(
        {
            "https://a.com/robots.txt": robots(NEWS_A),
            NEWS_A: news_sitemap(*((f"n{i}", 1) for i in range(12)), host="https://a.com"),
            "https://a.com": b'<link rel="alternate" type="application/rss+xml" href="/feed">',
            "https://a.com/feed": rss(("f1", 1), ("f2", 2), host="https://a.com"),
            "https://b.com/robots.txt": status("https://b.com/robots.txt", 403),
        }
    )


@pytest.fixture
def sites(tmp_path: Path) -> Path:
    path = tmp_path / "sites.csv"
    path.write_text(SITES, encoding="utf-8")
    return path


def probe_sources(sites: Path, *extra: str, fetcher: FakeFetcher | None = None) -> int:
    return main(
        ["probe-sources", "--sites", str(sites), "--hints", "/nao/existe.csv", *extra],
        fetcher=fetcher or world(),
        now=NOW,
    )


def test_parser_tem_padroes_conservadores():
    args = build_parser().parse_args(["probe-sources"])

    assert args.max_requests == 40
    assert args.dry_run is False
    assert args.hints.name == "discovery_hints.csv"
    assert args.workers == 8


def test_grava_o_principal_e_a_reserva_da_fonte_que_serve(
    sites: Path, capsys: pytest.CaptureFixture[str]
):
    code = probe_sources(sites)

    out = capsys.readouterr().out
    assert code == 0
    chosen = {r.role: r for r in StateStore().site_strategies("a.com") if r.role}
    assert chosen[Role.PRIMARY].url == NEWS_A
    assert chosen[Role.PRIMARY].kind is StrategyKind.NEWS_SITEMAP
    assert chosen[Role.BACKUP].url == "https://a.com/feed"
    assert "a.com" in out and "news_sitemap" in out


def test_fonte_sem_estrategia_aparece_com_o_motivo_e_nao_apaga_a_anterior(
    sites: Path, capsys: pytest.CaptureFixture[str]
):
    probe_sources(sites)
    capsys.readouterr()

    probe_sources(sites)  # B continua bloqueada; A continua boa

    out = capsys.readouterr().out
    assert "b.com" in out and "blocked" in out
    assert StateStore().site_strategies("b.com") == []


def test_falha_numa_nova_sondagem_nao_apaga_a_estrategia_que_ja_servia(sites: Path):
    probe_sources(sites)
    broken = FakeFetcher({"https://a.com/robots.txt": status("https://a.com/robots.txt", 403)})

    probe_sources(sites, "--only", "a.com", fetcher=broken)

    assert [r.url for r in StateStore().site_strategies("a.com") if r.role is Role.PRIMARY] == [
        NEWS_A
    ]


def test_dry_run_nao_grava(sites: Path, capsys: pytest.CaptureFixture[str]):
    code = probe_sources(sites, "--dry-run")

    assert code == 0
    assert StateStore().site_strategies("a.com") == []
    assert "news_sitemap" in capsys.readouterr().out


def test_usa_as_dicas_do_csv(sites: Path, tmp_path: Path):
    hints = tmp_path / "dicas.csv"
    hints.write_text(
        "nome,dominio,news_sitemap_url,rss_url,sitemap_url\n"
        "A,a.com,https://a.com/outro-news.xml,,\n",
        encoding="utf-8",
    )
    fetcher = world()
    fetcher.responses["https://a.com/outro-news.xml"] = news_sitemap(
        *((f"x{i}", 1) for i in range(50)), host="https://a.com"
    )

    code = main(
        ["probe-sources", "--sites", str(sites), "--hints", str(hints), "--only", "a.com"],
        fetcher=fetcher,
        now=NOW,
    )

    assert code == 0
    primary = next(r for r in StateStore().site_strategies("a.com") if r.role is Role.PRIMARY)
    assert primary.url == "https://a.com/outro-news.xml"  # mais cobertura que o do robots


def test_resumo_conta_fontes_por_tipo_de_principal(sites: Path, capsys: pytest.CaptureFixture[str]):
    probe_sources(sites)

    out = capsys.readouterr().out
    assert "fontes: 2" in out and "com estratégia: 1" in out and "news_sitemap: 1" in out


def test_so_as_fontes_pedidas(sites: Path):
    probe_sources(sites, "--only", "b.com")

    assert StateStore().site_strategies("a.com") == []


def test_estrategia_sem_data_aparece_como_undated_e_nao_como_zero_recentes(
    sites: Path, capsys: pytest.CaptureFixture[str]
):
    fetcher = FakeFetcher(
        {
            "https://a.com/robots.txt": robots("https://a.com/s.xml"),
            "https://a.com/s.xml": plain_sitemap(("x", None), ("y", None), host="https://a.com"),
        }
    )

    probe_sources(sites, "--only", "a.com", "--dry-run", fetcher=fetcher)

    out = capsys.readouterr().out
    assert "undated" in out and "0 recentes" not in out


# --- reavaliação por saúde (--due) ---


def seed_strategy(site: str, url: str, *, probed_at=None, role=Role.PRIMARY) -> None:
    from datetime import timedelta

    from buscador_midias.domain.strategy import CandidateOrigin, CandidateReport, Verdict

    StateStore().replace_site_strategies(
        site,
        [
            CandidateReport(
                url=url,
                origin=CandidateOrigin.HINT,
                verdict=Verdict.FRESH,
                kind=StrategyKind.NEWS_SITEMAP,
                measure=None,
                score=10,
                detail="",
                role=role,
            )
        ],
        probed_at or NOW - timedelta(hours=1),
    )


def seed_state(site: str, **kw) -> None:
    from datetime import timedelta

    from buscador_midias.domain.strategy import ProbeState

    base = {
        "site_key": site,
        "probed_at": NOW - timedelta(hours=1),
        "found": True,
        "streak": 0,
        "cooldown_until": NOW - timedelta(hours=1),
        "refresh_at": NOW + timedelta(days=6),
    }
    base.update(kw)
    StateStore().record_probe_state(ProbeState(**base), "")


def seed_failing_rounds(site: str) -> None:
    from datetime import timedelta

    from buscador_midias.state.store import SiteRunRecord

    store = StateStore()
    for n in range(2):
        run = store.start_run(NOW - timedelta(minutes=30 - n))
        store.record_site_run(run, SiteRunRecord(site, "error", 0, 0, 0, 0, 1, 0, "x"))


def test_due_sonda_so_quem_esta_na_vez_e_diz_por_que(
    sites: Path, capsys: pytest.CaptureFixture[str]
):
    seed_strategy("a.com", NEWS_A)
    seed_state("a.com")  # em dia, sem falhas

    fetcher = world()
    code = probe_sources(sites, "--due", fetcher=fetcher)

    out = capsys.readouterr().out
    assert code == 0
    assert not any(call.startswith("https://a.com") for call in fetcher.calls)  # a.com em dia
    assert any(call.startswith("https://b.com") for call in fetcher.calls)  # b.com nunca sondada
    assert "never_probed" in out and "em dia: 1" in out


def test_due_com_falhas_seguidas_reavalia_e_a_mesma_escolha_aumenta_a_espera(sites: Path):
    seed_strategy("a.com", NEWS_A)
    seed_state("a.com")
    seed_failing_rounds("a.com")

    probe_sources(sites, "--due", "--only", "a.com")

    state = StateStore().probe_states()["a.com"]
    assert state.found and state.streak == 1  # reavaliou por saúde e nada mudou
    assert state.cooldown_until > NOW
    assert state.probed_at == NOW


def test_due_com_falhas_e_escolha_nova_zera_a_espera(sites: Path):
    seed_strategy("a.com", "https://a.com/antigo.xml")
    seed_state("a.com")
    seed_failing_rounds("a.com")

    probe_sources(sites, "--due", "--only", "a.com")

    store = StateStore()
    primary = next(r for r in store.site_strategies("a.com") if r.role is Role.PRIMARY)
    assert primary.url == NEWS_A and store.probe_states()["a.com"].streak == 0


def test_estrategia_antiga_sem_estado_e_adotada_e_nao_sondada_de_novo(sites: Path):
    seed_strategy("a.com", NEWS_A)  # gravada pela fatia 1, antes de existir o estado

    fetcher = world()
    probe_sources(sites, "--due", "--only", "a.com", fetcher=fetcher)

    assert fetcher.calls == []


def test_estrategia_antiga_sem_estado_e_velha_e_reavaliada_por_idade(sites: Path):
    from datetime import timedelta

    seed_strategy("a.com", NEWS_A, probed_at=NOW - timedelta(days=8))

    fetcher = world()
    probe_sources(sites, "--due", "--only", "a.com", fetcher=fetcher)

    assert any(call.startswith("https://a.com") for call in fetcher.calls)
    assert StateStore().probe_states()["a.com"].found


def test_fonte_que_nao_achou_nada_fica_com_espera_crescente_e_nao_e_sondada_logo_de_novo(
    sites: Path,
):
    probe_sources(sites, "--only", "b.com")  # b.com bloqueia o robots.txt

    state = StateStore().probe_states()["b.com"]
    assert not state.found and state.streak == 1

    fetcher = world()
    probe_sources(sites, "--due", "--only", "b.com", fetcher=fetcher)
    assert fetcher.calls == []  # ainda esperando


def test_sondagem_manual_tambem_grava_o_estado_e_dry_run_nao(sites: Path):
    probe_sources(sites, "--dry-run")
    assert StateStore().probe_states() == {}

    probe_sources(sites)

    assert set(StateStore().probe_states()) == {"a.com", "b.com"}
