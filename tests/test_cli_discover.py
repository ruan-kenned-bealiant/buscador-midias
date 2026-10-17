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
    return {"sites": sites, "dir": tmp_path}


def discover(
    ws: dict[str, Path], *extra: str, fetcher: object | None = None, **kwargs: object
) -> int:
    return main(
        ["discover", "--sites", str(ws["sites"]), *extra],
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

    store = StateStore()

    assert [r.id for r in store.list_runs()] == [1]
    assert store.count_urls() == 6


def test_only_file_ignora_comentarios_e_linhas_em_branco(workspace: dict[str, Path]):
    keys = workspace["dir"] / "amostra.txt"
    keys.write_text("# amostra da POC\n\nb.com\n  c.com  \n", encoding="utf-8")
    world = World()

    code = discover(workspace, "--only-file", str(keys), fetcher=world)

    assert code == 0
    assert not any("a.com" in call for call in world.calls)
    assert {r.site_key for r in StateStore().site_runs(1)} == {"b.com", "c.com"}


def test_only_e_only_file_se_somam(workspace: dict[str, Path]):
    keys = workspace["dir"] / "amostra.txt"
    keys.write_text("b.com\n", encoding="utf-8")

    discover(workspace, "--only-file", str(keys), "--only", "c.com", fetcher=World())

    assert {r.site_key for r in StateStore().site_runs(1)} == {"b.com", "c.com"}


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

    code = main(["report"])

    out = capsys.readouterr().out
    assert code == 0
    assert "rodada 1" in out
    assert "rodada 2" in out


def test_report_sem_rodadas_nao_quebra(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    code = main(["report"])

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
    assert StateStore().list_runs() == []


def test_corpus_sem_variaveis_aborta_sem_criar_rodada(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import os

    connection = dict(os.environ)  # o estado segue acessível para conferir que nada foi criado
    for name in ("HOST", "PORT", "DATABASE", "USERNAME", "PASSWORD"):
        monkeypatch.delenv(f"DB_MIDIAS_IA_{name}", raising=False)

    code = discover(workspace, "--corpus", fetcher=World())

    assert code == 2
    assert "DB_MIDIAS_IA_HOST" in capsys.readouterr().err
    assert StateStore(connection).list_runs() == []


def test_corpus_fora_do_ar_nao_derruba_a_descoberta(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from buscador_midias.corpus.index import PostgresCorpusIndex

    def unreachable(env, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(PostgresCorpusIndex, "from_env", staticmethod(unreachable))

    code = discover(workspace, "--corpus", fetcher=World())

    captured = capsys.readouterr()
    assert code == 0
    assert "corpus indisponível (ConnectionError)" in captured.err
    assert len(StateStore().list_runs()) == 1


def test_host_bloqueado_fica_persistido_e_nao_tenta_o_sitemap_gratuito_de_novo(
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
    assert StateStore().fallback_hosts() == frozenset({"bloqueado.com"})
    assert any(c.startswith("DIRETO") for c in calls)

    calls.clear()
    assert run_once() == 0

    assert not any(c.startswith("DIRETO") and "sitemap" in c for c in calls)
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
    assert number(out, "filtradas (forma)") == 1


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
    assert number(out, "filtradas (forma)") == 1


def test_parser_do_painel_escuta_so_na_maquina_local_por_padrao():
    args = build_parser().parse_args(["dashboard"])

    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.schedule.name == "schedule.json"


def test_painel_sem_conexao_com_o_estado_aborta_com_mensagem(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DB_MIDIAS_IA_PORT", "1")  # nada escuta nessa porta

    code = main(["dashboard", "--port", "0"], serve=lambda server: None)

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

    code = main(["dashboard", "--port", "0"], serve=serve)

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

    [record] = StateStore().list_runs()
    assert record.bright_data_requests == 2  # robots.txt e sitemap
    assert record.bright_data_hosts == 1


def test_rodada_sem_bright_data_grava_zero(workspace: dict[str, Path]):
    assert discover(workspace, fetcher=World()) == 0

    [record] = StateStore().list_runs()
    assert record.bright_data_requests == 0


def feed_xml(*links: str) -> bytes:
    items = "".join(f"<item><title>T</title><link>{link}</link></item>" for link in links)
    return f'<rss version="2.0"><channel>{items}</channel></rss>'.encode()


def test_cli_une_complementos_persistidos_e_registra_novidade_uma_vez(workspace, capsys):
    from tests.discovery.test_sources import strategy
    from tests.strategy.helpers import FakeFetcher

    from buscador_midias.domain.strategy import Role, StrategyKind
    from buscador_midias.state.store import UrlKind

    store = StateStore()
    store.replace_site_strategies('a.com', [
        strategy('https://a.com/main.xml', StrategyKind.SITEMAP, Role.PRIMARY),
        strategy('https://a.com/feed', StrategyKind.FEED, Role.COMPLEMENT),
    ], NOW)
    f = FakeFetcher({'https://a.com/robots.txt': b'',
                     'https://a.com/main.xml': sitemap('https://a.com/noticia-um'),
                     'https://a.com/feed': feed_xml('https://a.com/noticia-um/',
                                                   'https://a.com/noticia-dois')})
    assert discover(workspace, '--only', 'a.com', fetcher=f) == 0
    assert store.site_runs(1)[0].baseline_urls == 2
    assert store.site_runs(1)[0].source == 'combined'
    f.responses['https://a.com/feed'] = feed_xml('https://a.com/noticia-dois',
                                               'https://a.com/noticia-tres')
    assert discover(workspace, '--only', 'a.com', fetcher=f) == 0
    assert store.site_runs(2)[0].new_urls == 1
    assert discover(workspace, '--only', 'a.com', fetcher=f) == 0
    assert store.site_runs(3)[0].new_urls == 0
    assert store.count_urls(kind=UrlKind.NEW) == 1
    assert "sitemap 0, feed 0, sitemap pago 0, conjunto 1" in capsys.readouterr().out
    store.close()


def test_plano_padrao_e_aplicado_ao_catalogo_e_no_strategy_o_desliga(workspace, monkeypatch):
    from tests.strategy.helpers import FakeFetcher

    import buscador_midias.cli as cli

    path = workspace['dir'] / 'plano.json'
    path.write_text(json.dumps([{'chave': 'a.com', 'mecanismos': [
        {'url': 'https://a.com/planejado.xml', 'kind': 'news_sitemap'},
        {'url': 'https://a.com/planejado-feed', 'kind': 'feed'},
    ]}]))
    monkeypatch.setattr(cli, 'DEFAULT_SITES', workspace['sites'])
    monkeypatch.setattr(cli, 'DEFAULT_DISCOVERY_PLAN', path)
    f = FakeFetcher({'https://a.com/robots.txt': b'',
                     'https://a.com/planejado.xml': sitemap('https://a.com/noticia-um'),
                     'https://a.com/planejado-feed': feed_xml('https://a.com/noticia-dois')})
    assert discover(workspace, '--only', 'a.com', fetcher=f) == 0
    assert 'https://a.com/planejado-feed' in f.calls
    f.calls.clear()
    assert discover(workspace, '--only', 'a.com', '--no-strategy', fetcher=f) == 0
    assert 'https://a.com/planejado.xml' not in f.calls
    assert 'https://a.com/planejado-feed' not in f.calls


def test_plano_explicito_invalido_nao_inicia_rodada(workspace):
    path = workspace['dir'] / 'plano.json'
    path.write_text(json.dumps([{'chave': 'a.com', 'mecanismos': [
        {'url': 'https://fora.com/feed', 'kind': 'feed'},
    ]}]))
    f = World()
    assert discover(workspace, '--discovery-plan', str(path), fetcher=f) == 2
    assert f.calls == []
    store = StateStore()
    assert store.list_runs() == []
    store.close()


def test_migracao_ausente_impede_http_e_criacao_de_rodada(workspace):
    from tests.discovery.test_sources import strategy
    from tests.strategy.helpers import FakeFetcher

    from buscador_midias.domain.strategy import Role, StrategyKind

    store = StateStore()
    store.replace_site_strategies('a.com', [
        strategy('https://a.com/main.xml', StrategyKind.SITEMAP, Role.PRIMARY),
        strategy('https://a.com/feed', StrategyKind.FEED, Role.COMPLEMENT),
    ], NOW)
    store._conn.execute(
        "ALTER TABLE midias_crawler_site_runs DROP CONSTRAINT midias_crawler_site_runs_source_check"
    )
    store._conn.execute(
        "ALTER TABLE midias_crawler_site_runs ADD CONSTRAINT midias_crawler_site_runs_source_check"
        " CHECK (source IN ('', 'sitemap', 'feed', 'sitemap_bd'))"
    )
    f = FakeFetcher()
    assert discover(workspace, '--only', 'a.com', fetcher=f) == 2
    assert f.calls == [] and store.list_runs() == []
    store.close()


def test_site_sem_sitemap_e_lido_pelo_feed_e_o_resumo_mostra_as_fontes(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    workspace["sites"].write_text(
        "region,name,url,country,country_code,language,source_type\n"
        "Brasil,Feed,https://feed.com,Brasil,BR,Português,Jornal\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed/":
            return httpx.Response(200, content=feed_xml("https://feed.com/materia-um"))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    assert discover(workspace, "--interval", "0", client=client) == 0

    out = capsys.readouterr().out
    assert "fontes: sitemap 0, feed 1, sitemap pago 0" in out
    [record] = StateStore().site_runs(1)
    assert (record.status, record.source, record.baseline_urls) == ("ok", "feed", 1)


def test_no_feed_desliga_a_alternativa(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    workspace["sites"].write_text(
        "region,name,url,country,country_code,language,source_type\n"
        "Brasil,Feed,https://feed.com,Brasil,BR,Português,Jornal\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed/":
            return httpx.Response(200, content=feed_xml("https://feed.com/materia-um"))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    assert discover(workspace, "--no-feed", "--interval", "0", client=client) == 0

    [record] = StateStore().site_runs(1)
    assert (record.status, record.source) == ("no_sitemap", "")
    assert build_parser().parse_args(["discover"]).no_feed is False


class StrategyWorld(World):
    """O robots declara /sitemap.xml, mas a estratégia gravada aponta para /estrategia.xml."""

    def __init__(self) -> None:
        super().__init__()
        self.strategy_pages = ["https://a.com/e1", "https://a.com/e2"]

    def get(self, url: str) -> FetchResult:
        if url == "https://a.com/estrategia.xml":
            self.calls.append(url)
            return FetchResult(url, 200, sitemap(*self.strategy_pages), None)
        return super().get(url)


def store_strategy(url: str = "https://a.com/estrategia.xml") -> None:
    from buscador_midias.domain.strategy import (
        CandidateOrigin,
        CandidateReport,
        Role,
        StrategyKind,
        Verdict,
    )

    StateStore().replace_site_strategies(
        "a.com",
        [
            CandidateReport(
                url=url,
                origin=CandidateOrigin.HINT,
                verdict=Verdict.FRESH,
                kind=StrategyKind.NEWS_SITEMAP,
                measure=None,
                score=10,
                detail="",
                role=Role.PRIMARY,
            )
        ],
        NOW,
    )


def test_descoberta_usa_a_estrategia_gravada_da_fonte(workspace: dict[str, Path]):
    world = StrategyWorld()
    store_strategy()

    assert discover(workspace, "--only", "a.com", fetcher=world) == 0

    assert "https://a.com/estrategia.xml" in world.calls
    assert "https://a.com/sitemap.xml" not in world.calls  # o plano B nem foi preciso
    rows = StateStore()._all("SELECT url FROM midias_crawler_urls WHERE site_key = 'a.com'")
    assert {r[0] for r in rows} == {"https://a.com/e1", "https://a.com/e2"}


def test_estrategia_quebrada_cai_no_plano_b_e_a_fonte_nao_fica_sem_descoberta(
    workspace: dict[str, Path],
):
    world = StrategyWorld()
    store_strategy("https://a.com/sumiu.xml")

    assert discover(workspace, "--only", "a.com", fetcher=world) == 0

    assert "https://a.com/sumiu.xml" in world.calls
    rows = StateStore()._all("SELECT url FROM midias_crawler_urls WHERE site_key = 'a.com'")
    assert {r[0] for r in rows} == {"https://a.com/1", "https://a.com/2"}  # do sitemap do robots


def test_no_strategy_ignora_a_estrategia_gravada(workspace: dict[str, Path]):
    world = StrategyWorld()
    store_strategy()

    assert discover(workspace, "--only", "a.com", "--no-strategy", fetcher=world) == 0

    assert "https://a.com/estrategia.xml" not in world.calls


def test_parser_do_discover_usa_a_estrategia_por_padrao():
    assert build_parser().parse_args(["discover"]).no_strategy is False
    assert build_parser().parse_args(["run"]).no_strategy is False
