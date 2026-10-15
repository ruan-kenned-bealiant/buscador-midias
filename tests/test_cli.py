import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from buscador_midias.cli import build_parser, main
from buscador_midias.net.fetcher import FetchResult

SITES = (
    "region,name,url,country,country_code,language,source_type\n"
    "Brasil,A,https://a.com,Brasil,BR,Português,Jornal\n"
    "Brasil,B,https://b.com,Brasil,BR,Português,Jornal\n"
    "Brasil,C,https://c.com,Brasil,BR,Português,Jornal\n"
)


class NotFoundFetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        return FetchResult(url=url, status=404, content=b"", error=None)


def test_parser_tem_padroes_conservadores():
    args = build_parser().parse_args(["probe"])

    assert args.limit is None
    assert args.workers == 8
    assert args.interval == 1.0
    assert args.sites.name == "sites.csv"


def test_bright_data_e_opt_in_com_teto_padrao():
    args = build_parser().parse_args(["probe"])

    assert args.bright_data is False
    assert args.bright_data_max_requests == 500


def test_bright_data_sem_credenciais_aborta_antes_de_qualquer_requisicao(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.delenv("BRIGHTDATA_API_KEY", raising=False)
    monkeypatch.delenv("BRIGHTDATA_ZONE_UNLOCKER", raising=False)
    sites = tmp_path / "sites.csv"
    sites.write_text(SITES, encoding="utf-8")

    code = main(["probe", "--sites", str(sites), "--out", str(tmp_path / "r.csv"), "--bright-data"])

    assert code == 2
    assert "BRIGHTDATA_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "r.csv").exists()


def test_parser_exige_um_subcomando():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


def run(tmp_path: Path, *extra: str, fetcher: NotFoundFetcher | None = None) -> int:
    sites = tmp_path / "sites.csv"
    sites.write_text(SITES, encoding="utf-8")
    return main(
        ["probe", "--sites", str(sites), "--out", str(tmp_path / "r.csv"), *extra],
        fetcher=fetcher or NotFoundFetcher(),
        now=datetime(2026, 10, 15, tzinfo=UTC),
    )


def test_only_sonda_apenas_as_chaves_pedidas(tmp_path: Path):
    fetcher = NotFoundFetcher()

    code = run(tmp_path, "--only", "c.com", "a.com", fetcher=fetcher)

    assert code == 0
    rows = list(csv.DictReader((tmp_path / "r.csv").open(encoding="utf-8")))
    assert [r["site_key"] for r in rows] == ["a.com", "c.com"]
    assert not any("b.com" in call for call in fetcher.calls)


def test_only_com_chave_desconhecida_falha_sem_requisicoes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    fetcher = NotFoundFetcher()

    code = run(tmp_path, "--only", "a.com", "nao-existe.com", fetcher=fetcher)

    assert code == 2
    assert "nao-existe.com" in capsys.readouterr().err
    assert fetcher.calls == []
    assert not (tmp_path / "r.csv").exists()


def test_progresso_vai_para_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    run(tmp_path, "--progress-every", "1")

    captured = capsys.readouterr()
    assert "1/3" in captured.err
    assert "3/3" in captured.err
    assert "1/3" not in captured.out


def test_probe_respeita_limit_grava_relatorio_e_imprime_resumo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    sites = tmp_path / "sites.csv"
    sites.write_text(SITES, encoding="utf-8")
    out = tmp_path / "relatorio.csv"
    fetcher = NotFoundFetcher()

    code = main(
        ["probe", "--sites", str(sites), "--out", str(out), "--limit", "2"],
        fetcher=fetcher,
        now=datetime(2026, 10, 15, tzinfo=UTC),
    )

    assert code == 0
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["site_key"] for r in rows] == ["a.com", "b.com"]
    assert {r["status"] for r in rows} == {"no_sitemap"}
    assert not any("c.com" in call for call in fetcher.calls)
    printed = capsys.readouterr().out
    assert "total" in printed
    assert "no_sitemap" in printed
