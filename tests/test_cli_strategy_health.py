"""Relatório de quebras: quais fontes tiveram o padrão de leitura quebrado."""

from datetime import timedelta
from pathlib import Path

import pytest
from tests.strategy.helpers import NOW

from buscador_midias.cli import build_parser, main
from buscador_midias.domain.strategy import (
    CandidateOrigin,
    CandidateReport,
    Measure,
    ProbeState,
    Role,
    StrategyKind,
    Verdict,
)
from buscador_midias.state.store import SiteRunRecord, StateStore

SITES = (
    "region,name,url,country,country_code,language,source_type\n"
    "Brasil,A,https://a.com,Brasil,BR,Português,Jornal\n"
    "Brasil,B,https://b.com,Brasil,BR,Português,Jornal\n"
    "Brasil,C,https://c.com,Brasil,BR,Português,Jornal\n"
)


@pytest.fixture
def sites(tmp_path: Path) -> Path:
    path = tmp_path / "sites.csv"
    path.write_text(SITES, encoding="utf-8")
    return path


def strategy(url: str) -> CandidateReport:
    return CandidateReport(
        url,
        CandidateOrigin.ROBOTS,
        Verdict.FRESH,
        StrategyKind.FEED,
        Measure(StrategyKind.FEED, 10, 10, 10, 10, NOW, 100),
        9.0,
        "",
        role=Role.PRIMARY,
    )


def seed() -> None:
    """A: padrão quebrou (2 falhas depois da sondagem). B: saudável. C: nunca teve padrão."""
    store = StateStore()
    probed = NOW - timedelta(days=2)
    for key in ("a.com", "b.com"):
        store.replace_site_strategies(key, [strategy(f"https://{key}/feed")], probed)
        store.record_probe_state(
            ProbeState(key, probed, True, 0, probed, probed + timedelta(days=7)), ""
        )
    store.record_probe_state(
        ProbeState("c.com", probed, False, 1, NOW, NOW + timedelta(days=1), "no_sitemap: x"), ""
    )
    for hours in (30, 18, 6):
        run = store.start_run(NOW - timedelta(hours=hours))
        store.record_site_run(run, SiteRunRecord("a.com", "error", 0, 0, 0, 0, 1, 0, ""))
        store.record_site_run(run, SiteRunRecord("b.com", "ok", 5, 0, 0, 100, 1, 0, ""))


def health(sites: Path, *extra: str) -> int:
    return main(["strategy-health", "--sites", str(sites), *extra], now=NOW)


def test_lista_so_o_padrao_quebrado_e_diz_quantas_estao_em_dia(
    sites: Path, capsys: pytest.CaptureFixture[str]
):
    seed()

    code = health(sites)

    out = capsys.readouterr().out
    assert code == 0
    assert "QUEBROU" in out and "a.com" in out
    assert "b.com" not in out.split("EM DIA")[0]  # a saudável não aparece entre os problemas
    assert "c.com" not in out.split("EM DIA")[0]  # sem estratégia não é quebra
    assert "1 quebrou" in out and "1 em dia" in out and "1 sem estratégia" in out


def test_csv_traz_uma_linha_por_fonte_com_problema(sites: Path, tmp_path: Path):
    seed()
    out = tmp_path / "saude.csv"

    health(sites, "--out", str(out))

    lines = out.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].startswith("Chave,Fonte,Situação")
    assert len(lines) == 2 and lines[1].startswith("a.com,A,quebrou")


def test_sem_nada_gravado_diz_que_nao_ha_o_que_avaliar(
    sites: Path, capsys: pytest.CaptureFixture[str]
):
    health(sites)

    assert "nenhuma fonte com padrão gravado" in capsys.readouterr().out


def test_parser_aceita_rodadas_e_saida():
    args = build_parser().parse_args(["strategy-health", "--rounds", "8", "--out", "x.csv"])

    assert args.rounds == 8 and args.out == Path("x.csv")
