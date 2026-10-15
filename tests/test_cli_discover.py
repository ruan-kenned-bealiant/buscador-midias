import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from buscador_midias.cli import build_parser, main
from buscador_midias.domain.url import url_hash
from buscador_midias.net.fetcher import FetchResult
from buscador_midias.state.store import StateStore

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SITES = (
    "region,name,url,country,country_code,language,source_type\n"
    "Brasil,A,https://a.com,Brasil,BR,Português,Jornal\n"
    "Brasil,B,https://b.com,Brasil,BR,Português,Jornal\n"
    "Brasil,C,https://c.com,Brasil,BR,Português,Jornal\n"
)


def sitemap(*locs: str) -> bytes:
    urls = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode()


class World:
    """Três sites com sitemap simples; o conteúdo pode crescer entre as rodadas."""

    def __init__(self) -> None:
        hosts = ("a.com", "b.com", "c.com")
        self.pages = {host: [f"https://{host}/1", f"https://{host}/2"] for host in hosts}
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        host = url.split("/")[2]
        if url.endswith("/robots.txt"):
            body = f"Sitemap: https://{host}/sitemap.xml\n".encode()
        elif url.endswith("/sitemap.xml") and host in self.pages:
            body = sitemap(*self.pages[host])
        else:
            return FetchResult(url=url, status=404, content=b"", error=None)
        return FetchResult(url=url, status=200, content=body, error=None)


class FakeCorpus:
    def __init__(self, known: set[str]) -> None:
        self.known = known

    def known_hashes(self, hashes: list[str]) -> set[str]:
        return {h for h in hashes if h in self.known}


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    sites = tmp_path / "sites.csv"
    sites.write_text(SITES, encoding="utf-8")
    return {"sites": sites, "db": tmp_path / "estado.db", "dir": tmp_path}


def discover(
    ws: dict[str, Path], *extra: str, fetcher: object | None = None, **kwargs: object
) -> int:
    return main(
        ["discover", "--sites", str(ws["sites"]), "--db", str(ws["db"]), *extra],
        fetcher=fetcher,  # type: ignore[arg-type]
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def number(out: str, label: str) -> int:
    match = re.search(rf"{re.escape(label)}:\s+(\d+)", out)
    assert match, f"{label!r} não encontrado em:\n{out}"
    return int(match.group(1))


def test_parser_do_discover_tem_padroes_conservadores():
    args = build_parser().parse_args(["discover"])

    assert args.db.name == "state.db"
    assert args.fresh_days == 7
    assert args.max_sitemap_fetches == 6
    assert args.corpus is False
    assert args.only_file is None


def test_primeira_rodada_e_linha_de_base_e_a_segunda_acha_as_novas(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    world = World()
    assert discover(workspace, fetcher=world) == 0
    first = capsys.readouterr().out
    world.pages["a.com"].append("https://a.com/3")
    world.pages["b.com"].append("https://b.com/3")

    assert discover(workspace, fetcher=world) == 0
    second = capsys.readouterr().out

    assert number(first, "linha de base") == 6
    assert number(first, "novas") == 0
    assert number(second, "novas") == 2
    assert number(second, "linha de base") == 0
    assert number(second, "sites") == 3


def test_estado_persiste_entre_execucoes_no_arquivo(workspace: dict[str, Path]):
    discover(workspace, fetcher=World())

    store = StateStore(workspace["db"])

    assert [r.id for r in store.list_runs()] == [1]
    assert store.count_urls() == 6


def test_only_file_ignora_comentarios_e_linhas_em_branco(workspace: dict[str, Path]):
    keys = workspace["dir"] / "amostra.txt"
    keys.write_text("# amostra da POC\n\nb.com\n  c.com  \n", encoding="utf-8")
    world = World()

    code = discover(workspace, "--only-file", str(keys), fetcher=world)

    assert code == 0
    assert not any("a.com" in call for call in world.calls)
    assert {r.site_key for r in StateStore(workspace["db"]).site_runs(1)} == {"b.com", "c.com"}


def test_only_e_only_file_se_somam(workspace: dict[str, Path]):
    keys = workspace["dir"] / "amostra.txt"
    keys.write_text("b.com\n", encoding="utf-8")

    discover(workspace, "--only-file", str(keys), "--only", "c.com", fetcher=World())

    assert {r.site_key for r in StateStore(workspace["db"]).site_runs(1)} == {"b.com", "c.com"}


def test_chave_desconhecida_no_arquivo_aborta_sem_criar_rodada(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    keys = workspace["dir"] / "amostra.txt"
    keys.write_text("b.com\nnao-existe.com\n", encoding="utf-8")

    code = discover(workspace, "--only-file", str(keys), fetcher=World())

    assert code == 2
    assert "nao-existe.com" in capsys.readouterr().err


def test_arquivo_de_amostra_inexistente_aborta(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    code = discover(workspace, "--only-file", str(workspace["dir"] / "nada.txt"), fetcher=World())

    assert code == 2
    assert "nada.txt" in capsys.readouterr().err


def test_corpus_marca_como_conhecidas_as_urls_que_ele_ja_tem(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    world = World()
    discover(workspace, fetcher=world)
    capsys.readouterr()
    world.pages["a.com"] += ["https://a.com/no-corpus", "https://a.com/materia-inedita"]
    corpus = FakeCorpus({url_hash("https://a.com/no-corpus")})

    discover(workspace, fetcher=world, corpus=corpus)
    out = capsys.readouterr().out

    assert number(out, "novas") == 1
    assert number(out, "no corpus") == 1


def test_progresso_vai_para_stderr(workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]):
    discover(workspace, "--progress-every", "1", fetcher=World())

    captured = capsys.readouterr()
    assert "1/3" in captured.err
    assert "3/3" in captured.err


def test_report_mostra_as_rodadas(workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]):
    world = World()
    discover(workspace, fetcher=world)
    discover(workspace, fetcher=world)
    capsys.readouterr()

    code = main(["report", "--db", str(workspace["db"])])

    out = capsys.readouterr().out
    assert code == 0
    assert "rodada 1" in out
    assert "rodada 2" in out


def test_report_sem_banco_nao_cria_arquivo_nem_quebra(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    code = main(["report", "--db", str(workspace["db"])])

    assert code == 0
    assert "nenhuma rodada registrada" in capsys.readouterr().out


def test_bright_data_sem_credenciais_aborta_sem_criar_rodada(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.delenv("BRIGHTDATA_API_KEY", raising=False)
    monkeypatch.delenv("BRIGHTDATA_ZONE_UNLOCKER", raising=False)

    code = discover(workspace, "--bright-data")

    assert code == 2
    assert "BRIGHTDATA_API_KEY" in capsys.readouterr().err
    assert StateStore(workspace["db"]).list_runs() == []


def test_corpus_sem_variaveis_aborta_sem_criar_rodada(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    for name in ("HOST", "PORT", "DATABASE", "USERNAME", "PASSWORD"):
        monkeypatch.delenv(f"DB_MIDIAS_IA_{name}", raising=False)

    code = discover(workspace, "--corpus", fetcher=World())

    assert code == 2
    assert "DB_MIDIAS_IA_HOST" in capsys.readouterr().err
    assert StateStore(workspace["db"]).list_runs() == []


def test_host_bloqueado_fica_persistido_e_nao_tenta_o_acesso_gratuito_de_novo(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    monkeypatch.setenv("BRIGHTDATA_ZONE_UNLOCKER", "z")
    workspace["sites"].write_text(
        "region,name,url,country,country_code,language,source_type\n"
        "Brasil,Bloq,https://bloqueado.com,Brasil,BR,Português,Jornal\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.brightdata.com":
            target = json.loads(request.content)["url"]
            calls.append(f"UNLOCKER {target}")
            if target.endswith("/robots.txt"):
                return httpx.Response(200, content=b"Sitemap: https://bloqueado.com/sitemap.xml\n")
            return httpx.Response(200, content=sitemap("https://bloqueado.com/1"))
        calls.append(f"DIRETO {request.url}")
        return httpx.Response(403)

    def run_once() -> int:
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        return discover(workspace, "--bright-data", "--interval", "0", client=client)

    assert run_once() == 0
    assert StateStore(workspace["db"]).fallback_hosts() == frozenset({"bloqueado.com"})
    assert any(c.startswith("DIRETO") for c in calls)

    calls.clear()
    assert run_once() == 0

    assert not any(c.startswith("DIRETO") for c in calls)
    assert any(c.startswith("UNLOCKER") for c in calls)


def test_resumo_mostra_as_filtradas_pelo_l0(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    world = World()
    assert discover(workspace, fetcher=world) == 0
    capsys.readouterr()
    world.pages["a.com"] += ["https://a.com/tag/eleicoes", "https://a.com/politica/governo-cai-1"]

    assert discover(workspace, fetcher=world) == 0
    out = capsys.readouterr().out

    assert number(out, "novas") == 1
    assert number(out, "filtradas (L0)") == 1


def test_temas_fora_so_com_a_opcao_explicita(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    assert build_parser().parse_args(["discover"]).skip_off_topic is False
    world = World()
    assert discover(workspace, fetcher=world) == 0
    capsys.readouterr()
    world.pages["a.com"].append("https://a.com/esporte/futebol/clube-vence-o-classico")

    assert discover(workspace, "--skip-off-topic", fetcher=world) == 0
    out = capsys.readouterr().out

    assert number(out, "novas") == 0
    assert number(out, "filtradas (L0)") == 1


def test_l1_e_opcional_e_arquivo_inexistente_aborta_sem_criar_rodada(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    assert build_parser().parse_args(["discover"]).l1_model is None

    code = discover(
        workspace, "--l1-model", str(workspace["dir"] / "nao-existe.json"), fetcher=World()
    )

    assert code == 2
    assert "nao-existe.json" in capsys.readouterr().err
    assert not workspace["db"].exists()


def test_l1_descarta_pelo_modelo_informado(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    from buscador_midias.triage.l1 import L1Filter, NaiveBayesHeadlineModel

    model = NaiveBayesHeadlineModel.train(
        ["pf prende empresario por fraude e lavagem de dinheiro"] * 10,
        ["previsao do tempo hoje com sol e nuvens em recife"] * 10,
        min_count=2,
    )
    model_path = workspace["dir"] / "l1.json"
    L1Filter(model, threshold=0.0, languages=frozenset({"Português"})).save(model_path)
    world = World()
    assert discover(workspace, fetcher=world) == 0
    capsys.readouterr()
    world.pages["a.com"] += [
        "https://a.com/clima/previsao-do-tempo-hoje-com-sol-e-nuvens-em-recife",
        "https://a.com/policia/pf-prende-empresario-por-fraude-e-lavagem-de-dinheiro",
    ]

    assert discover(workspace, "--l1-model", str(model_path), fetcher=world) == 0
    out = capsys.readouterr().out

    assert number(out, "novas") == 1
    assert number(out, "filtradas (L0)") == 1


class FakeLlm:
    """Classificador falso: descarta manchetes com 'clima', mantém o resto."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def classify(self, items):
        from buscador_midias.triage.relevance import Decision, HeadlineVerdict

        self.texts += [item.text for item in items]
        return [
            HeadlineVerdict(
                item.id,
                Decision.DISCARD if "clima" in item.text else Decision.KEEP,
                (),
                "clima" if "clima" in item.text else "ok",
            )
            for item in items
        ]


def triage_cmd(ws: dict[str, Path], *extra: str, **kwargs: object) -> int:
    return main(["triage", "--db", str(ws["db"]), *extra], now=NOW, **kwargs)  # type: ignore[arg-type]


def grown_world(workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> World:
    world = World()
    assert discover(workspace, fetcher=world) == 0
    capsys.readouterr()
    world.pages["a.com"] += [
        "https://a.com/policia/pf-prende-empresario-por-fraude",
        "https://a.com/vida/previsao-do-clima-para-sexta-feira",
    ]
    return world


def test_parser_da_triagem_tem_padroes_baratos_e_permissivos():
    args = build_parser().parse_args(["triage"])

    assert args.model == "google/gemini-2.5-flash-lite"
    assert args.policy == "entities"
    assert args.batch_size == 25
    assert build_parser().parse_args(["discover"]).triage is False


def test_discover_com_triagem_classifica_as_novas_e_imprime_o_custo(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    world = grown_world(workspace, capsys)
    llm = FakeLlm()

    assert discover(workspace, "--triage", fetcher=world, classifier=llm) == 0
    out = capsys.readouterr().out

    assert sorted(llm.texts) == [
        "pf prende empresario por fraude",
        "previsao do clima para sexta feira",
    ]
    assert number(out, "triagem: mantidas") == 1
    assert number(out, "triagem: descartadas") == 1
    assert number(out, "triagem: pendentes") == 0
    store = StateStore(workspace["db"])
    assert store.triage_counts() == {"keep": 1, "discard": 1}


def test_discover_sem_a_opcao_nao_chama_o_llm(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    world = grown_world(workspace, capsys)
    llm = FakeLlm()

    assert discover(workspace, fetcher=world, classifier=llm) == 0

    assert llm.texts == []


def test_subcomando_triage_retoma_os_pendentes(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    world = grown_world(workspace, capsys)
    assert discover(workspace, fetcher=world) == 0
    capsys.readouterr()
    llm = FakeLlm()

    assert triage_cmd(workspace, classifier=llm) == 0
    out = capsys.readouterr().out

    assert number(out, "triagem: mantidas") == 1
    assert len(llm.texts) == 2
    assert triage_cmd(workspace, classifier=FakeLlm()) == 0
    assert number(capsys.readouterr().out, "triagem: manchetes") == 0


def test_triagem_sem_chave_do_openrouter_aborta_sem_criar_rodada(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert discover(workspace, "--triage", fetcher=World()) == 2

    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
    assert not workspace["db"].exists()


def test_subcomando_triage_sem_chave_aborta(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert triage_cmd(workspace) == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_report_mostra_triagem_e_audita_descartes(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    world = grown_world(workspace, capsys)
    assert discover(workspace, "--triage", fetcher=world, classifier=FakeLlm()) == 0
    capsys.readouterr()

    assert main(["report", "--db", str(workspace["db"]), "--audit", "5"]) == 0
    out = capsys.readouterr().out

    assert "triagem LLM" in out
    assert "amostra de descartes" in out
    assert "previsao-do-clima" in out
    assert "clima" in out


def test_parser_do_painel_escuta_so_na_maquina_local_por_padrao():
    args = build_parser().parse_args(["dashboard"])

    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.db.name == "state.db"
    assert args.schedule.name == "schedule.json"


def test_painel_sem_estado_aborta_com_mensagem(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    code = main(
        ["dashboard", "--db", str(workspace["db"]), "--port", "0"], serve=lambda server: None
    )

    assert code == 2
    assert "estado" in capsys.readouterr().err


def test_painel_sobe_o_servidor_e_mostra_o_endereco(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    assert discover(workspace, fetcher=World()) == 0
    capsys.readouterr()
    started: list[tuple[str, int]] = []

    def serve(server) -> None:
        started.append(server.server_address[:2])

    code = main(["dashboard", "--db", str(workspace["db"]), "--port", "0"], serve=serve)

    assert code == 0
    assert started[0][0] == "127.0.0.1"
    assert f"http://127.0.0.1:{started[0][1]}" in capsys.readouterr().out


def test_uso_do_bright_data_fica_gravado_na_rodada(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    monkeypatch.setenv("BRIGHTDATA_ZONE_UNLOCKER", "z")
    workspace["sites"].write_text(
        "region,name,url,country,country_code,language,source_type\n"
        "Brasil,Bloq,https://bloqueado.com,Brasil,BR,Português,Jornal\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.brightdata.com":
            target = json.loads(request.content)["url"]
            if target.endswith("/robots.txt"):
                return httpx.Response(200, content=b"Sitemap: https://bloqueado.com/sitemap.xml\n")
            return httpx.Response(200, content=sitemap("https://bloqueado.com/1"))
        return httpx.Response(403)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert discover(workspace, "--bright-data", "--interval", "0", client=client) == 0

    [record] = StateStore(workspace["db"]).list_runs()
    assert record.bright_data_requests == 2  # robots.txt e sitemap
    assert record.bright_data_hosts == 1


def test_rodada_sem_bright_data_grava_zero(workspace: dict[str, Path]):
    assert discover(workspace, fetcher=World()) == 0

    [record] = StateStore(workspace["db"]).list_runs()
    assert record.bright_data_requests == 0


def test_backup_copia_o_estado_com_data_no_nome(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    assert discover(workspace, fetcher=World()) == 0
    capsys.readouterr()
    out_dir = workspace["dir"] / "backups"

    code = main(["backup", "--db", str(workspace["db"]), "--out-dir", str(out_dir)], now=NOW)

    assert code == 0
    [saved] = list(out_dir.iterdir())
    assert saved.name == "state-20261015-120000.db"
    assert StateStore(saved, read_only=True).count_urls() > 0
    assert str(saved) in capsys.readouterr().out


def test_backup_sem_estado_aborta(workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]):
    code = main(
        ["backup", "--db", str(workspace["db"]), "--out-dir", str(workspace["dir"] / "b")], now=NOW
    )

    assert code == 2
    assert "estado" in capsys.readouterr().err
    assert not (workspace["dir"] / "b").exists()


def test_backup_repetido_no_mesmo_segundo_nao_sobrescreve(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    assert discover(workspace, fetcher=World()) == 0
    out_dir = workspace["dir"] / "backups"
    argv = ["backup", "--db", str(workspace["db"]), "--out-dir", str(out_dir)]
    assert main(argv, now=NOW) == 0
    capsys.readouterr()

    assert main(argv, now=NOW) == 2

    assert "já existe" in capsys.readouterr().err


def test_triage_retriage_discarded_reabre_so_os_descartes_de_outra_politica(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    world = grown_world(workspace, capsys)
    assert discover(workspace, fetcher=world) == 0
    assert triage_cmd(workspace, "--policy", "permissive", classifier=FakeLlm()) == 0
    capsys.readouterr()
    llm = FakeLlm()

    assert triage_cmd(workspace, "--retriage-discarded", classifier=llm) == 0
    out = capsys.readouterr().out

    assert llm.texts == ["previsao do clima para sexta feira"]  # só o descartado voltou
    assert number(out, "triagem: reabertas") == 1
    assert triage_cmd(workspace, "--retriage-discarded", classifier=FakeLlm()) == 0
    assert number(capsys.readouterr().out, "triagem: reabertas") == 0


def test_retriage_discarded_e_opt_in():
    assert build_parser().parse_args(["triage"]).retriage_discarded is False
