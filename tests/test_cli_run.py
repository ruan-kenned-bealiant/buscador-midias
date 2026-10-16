import fcntl
from datetime import UTC, datetime
from pathlib import Path

import pytest

from buscador_midias.analysis.context import ContextVerdict
from buscador_midias.cli import EnrichPorts, build_parser, main
from buscador_midias.corpus.article import CorpusArticle
from buscador_midias.corpus.writer import CorpusSchemaError, WriteOutcome
from buscador_midias.extraction.fetch import PageFetch, PageStatus
from buscador_midias.net.fetcher import FetchResult
from buscador_midias.state.store import StateStore

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SITES = (
    "region,name,url,country,country_code,language,source_type\n"
    "Brasil,A,https://a.com,Brasil,BR,Português,Jornal\n"
)
NEW_URL = "https://a.com/politica/pf-prende-empresario-por-fraude-em-licitacao"
TEXT = (
    "A Polícia Federal prendeu o empresário João Silva nesta quarta-feira por suspeita de fraude "
    "em licitação em contratos da prefeitura. Segundo a investigação, o esquema desviou recursos. "
)


def page_html() -> bytes:
    title = "PF prende empresário por fraude em licitação"
    body = "".join(f"<p>{TEXT}</p>" for _ in range(8))
    return (
        f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>{title}</title>'
        f'<meta property="og:type" content="article"></head>'
        f"<body><article><h1>{title}</h1>{body}</article></body></html>"
    ).encode()


def sitemap(*locs: str) -> bytes:
    urls = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode()


class World:
    """Um site com sitemap simples; o conteúdo cresce entre as execuções."""

    def __init__(self) -> None:
        self.pages = ["https://a.com/1", "https://a.com/2"]

    def get(self, url: str) -> FetchResult:
        if url.endswith("/robots.txt"):
            body = b"Sitemap: https://a.com/sitemap.xml\n"
        elif url.endswith("/sitemap.xml"):
            body = sitemap(*self.pages)
        else:
            return FetchResult(url=url, status=404, content=b"", error=None)
        return FetchResult(url=url, status=200, content=body, error=None)


class Articles:
    def fetch(self, url: str) -> PageFetch:
        return PageFetch(url, PageStatus.OK, 200, page_html(), "text/html", url)


class Analyzer:
    def analyze(self, items):
        return [
            ContextVerdict(
                i.id,
                True,
                True,
                ("fiscal",),
                ({"name": "João Silva", "type": "PF", "role": "acusado"},),
                "preso por fraude",
                "resumo",
                0.9,
                i.basis,
            )
            for i in items
        ]


class Memory:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def put(self, url_hash: str, content: bytes) -> str:
        path = f"midias-ia/evidencias/{url_hash}.html"
        self.files[path] = content
        return path


class Unlocker:
    def get(self, url: str) -> FetchResult:
        return FetchResult(url, 200, page_html(), None)


class FakeCorpus:
    def known_hashes(self, hashes: list[str]) -> set[str]:
        return set()


class FakeWriter:
    def __init__(self, schema_error: bool = False) -> None:
        self.corpus: dict[str, CorpusArticle] = {}
        self.schema_error = schema_error

    def check_schema(self) -> None:
        if self.schema_error:
            raise CorpusSchemaError("o corpus não tem: midias_ia_articles.ai_summary")

    def write(self, articles):
        out = []
        for article in articles:
            self.corpus[article.url_hash] = article
            out.append(WriteOutcome(article.url_hash, len(self.corpus), True, False, 1, 1))
        return out

    def existing(self, hashes):
        return {h for h in hashes if h in self.corpus}

    def source_names(self):
        return {}


@pytest.fixture
def ws(tmp_path: Path) -> dict[str, Path]:
    sites = tmp_path / "sites.csv"
    sites.write_text(SITES, encoding="utf-8")
    return {"sites": sites, "db": tmp_path / "estado.db", "dir": tmp_path}


def ports() -> EnrichPorts:
    return EnrichPorts(
        articles=Articles(), analyzer=Analyzer(), evidence_store=Memory(), bright_data=Unlocker()
    )


def run(ws: dict[str, Path], *extra: str, **deps) -> int:
    deps.setdefault("fetcher", World())
    deps.setdefault("corpus", FakeCorpus())
    deps.setdefault("enrich_ports", ports())
    deps.setdefault("corpus_writer", FakeWriter())
    return main(
        ["run", "--sites", str(ws["sites"]), "--db", str(ws["db"]), *extra], now=NOW, **deps
    )


def test_parser_do_run_tem_padroes_do_fluxo_agendado():
    args = build_parser().parse_args(["run"])

    assert args.db.name == "state.db"
    assert args.no_bright_data is False
    assert args.no_feed is False
    assert args.discovery_bright_data_max == 60
    assert args.enrich_bright_data_max == 100
    assert args.workers == 8
    assert args.enrich_workers == 8


def test_run_descobre_analisa_confere_o_criterio_captura_evidencia_e_grava_no_corpus(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    world, writer = World(), FakeWriter()
    assert run(ws, fetcher=world, corpus_writer=writer) == 0  # linha de base: nada de novo ainda
    assert writer.corpus == {}
    world.pages.append(NEW_URL)
    capsys.readouterr()

    code = run(ws, fetcher=world, corpus_writer=writer)

    out = capsys.readouterr().out
    assert code == 0
    store = StateStore(ws["db"], read_only=True)
    assert store.count_pending_enrichment() == 0
    assert store.corpus_write_counts() == {"written": 1, "created": 1}
    [article] = writer.corpus.values()
    assert article.url == NEW_URL
    assert out.index("DESCOBERTA") < out.index("ANÁLISE") < out.index("CORPUS")


def test_etapa_que_falha_nao_impede_as_seguintes_e_o_codigo_de_saida_avisa(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    writer = FakeWriter()

    code = run(ws, enrich_ports=EnrichPorts(), corpus_writer=writer)

    captured = capsys.readouterr()
    assert code == 1
    assert "ERRO: a análise terminou com código 2" in captured.out
    assert "CORPUS" in captured.out  # a gravação ainda foi tentada
    assert "ERRO: a gravação no corpus" not in captured.out
    assert "OPENROUTER_API_KEY" in captured.err


def test_falha_no_corpus_tambem_vira_codigo_de_saida(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    code = run(ws, corpus_writer=FakeWriter(schema_error=True))

    out = capsys.readouterr().out
    assert code == 1
    assert "ERRO: a gravação no corpus terminou com código 2" in out


def test_duas_execucoes_ao_mesmo_tempo_nao_se_atropelam(
    ws: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    ws["db"].parent.mkdir(parents=True, exist_ok=True)
    with (ws["db"].parent / "run.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        code = run(ws)

    assert code == 3
    assert "já existe uma execução em andamento" in capsys.readouterr().err
    assert not ws["db"].exists()


def test_o_lock_e_liberado_ao_terminar(ws: dict[str, Path]):
    assert run(ws) == 0
    assert run(ws) == 0


def test_com_bright_data_o_sitemap_bloqueado_e_resgatado_pelo_pago(ws: dict[str, Path]):
    class Blocked:
        def get(self, url: str) -> FetchResult:
            return FetchResult(url, 403, b"", None)

    code = run(ws, fetcher=Blocked(), paid_fetcher=World())

    assert code == 0
    [record] = StateStore(ws["db"], read_only=True).site_runs(1)
    assert (record.status, record.source) == ("ok", "sitemap_bd")


def test_no_bright_data_nao_gasta_nada_e_o_site_fica_com_a_falha(ws: dict[str, Path]):
    class Blocked:
        def get(self, url: str) -> FetchResult:
            return FetchResult(url, 403, b"", None)

    code = run(ws, "--no-bright-data", fetcher=Blocked(), paid_fetcher=World())

    assert code == 0  # um site bloqueado é resultado, não erro da execução
    [record] = StateStore(ws["db"], read_only=True).site_runs(1)
    assert (record.status, record.source) == ("blocked", "")


def test_as_etapas_recebem_as_opcoes_proprias(ws: dict[str, Path], monkeypatch: pytest.MonkeyPatch):
    from buscador_midias import cli

    seen: list[list[str]] = []
    real_main = cli.main

    def spy(argv, **kwargs):
        seen.append(list(argv))
        return real_main(argv, **kwargs)

    monkeypatch.setattr(cli, "main", spy)

    run(ws, "--workers", "3", "--enrich-workers", "12", "--skip-off-topic")

    by_step = {argv[0]: argv for argv in seen if argv[0] != "run"}
    discover, enrich = by_step["discover"], by_step["enrich"]
    assert discover[discover.index("--workers") + 1] == "3"
    assert enrich[enrich.index("--workers") + 1] == "12"
    assert "--skip-off-topic" in discover
