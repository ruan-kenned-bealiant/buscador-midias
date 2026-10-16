from datetime import UTC, datetime
from pathlib import Path

import pytest

from buscador_midias.analysis.context import ContextVerdict
from buscador_midias.cli import EnrichPorts, build_parser, main
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.extraction.fetch import PageFetch, PageStatus
from buscador_midias.net.fetcher import FetchResult
from buscador_midias.state.store import StateStore, UrlKind

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
TEXT = (
    "A Polícia Federal prendeu o empresário João Silva nesta quarta-feira por suspeita de fraude "
    "em licitação em contratos da prefeitura. Segundo a investigação, o esquema desviou recursos. "
)


def page_html(title: str = "PF prende empresário por fraude em licitação") -> bytes:
    body = "".join(f"<p>{TEXT}</p>" for _ in range(8))
    return (
        f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>{title}</title>'
        f'<meta property="og:type" content="article"></head>'
        f"<body><article><h1>{title}</h1>{body}</article></body></html>"
    ).encode()


URL = "https://a.com/politica/pf-prende-empresario-por-fraude-em-licitacao"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "estado.db"
    store = StateStore(path)
    run = store.start_run(NOW)
    store.record_urls(
        [DiscoveredUrl(URL, "h-pf", "a.com", "https://a.com/s.xml", None, None, None)],
        run_id=run,
        now=NOW,
        kind=UrlKind.NEW,
    )
    store.close()
    return path


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
    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def requests_made(self) -> int:
        return len(self.calls)

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        return FetchResult(url, 200, page_html(), None)


def enrich(db: Path, *extra: str, **ports):
    return main(["enrich", "--db", str(db), *extra], now=NOW, enrich_ports=EnrichPorts(**ports))


def test_parser_do_enrich_tem_padroes_baratos_e_reaproveita_o_html():
    args = build_parser().parse_args(["enrich"])

    assert args.model == "google/gemini-2.5-flash-lite"
    assert args.evidence_mode == "auto"
    assert args.evidence_dir.name == "evidence"
    assert args.batch_size == 6
    assert args.workers == 4
    assert args.bright_data_max_requests == 100


def test_enrich_analisa_e_reaproveita_o_html_do_download_direto(
    db: Path, capsys: pytest.CaptureFixture[str]
):
    memory, unlocker = Memory(), Unlocker()

    code = enrich(
        db, articles=Articles(), analyzer=Analyzer(), evidence_store=memory, bright_data=unlocker
    )

    out = capsys.readouterr().out
    assert code == 0
    store = StateStore(db, read_only=True)
    assert store.get_analysis("h-pf").finding is True  # type: ignore[union-attr]
    evidence = store.get_evidence("h-pf")
    assert evidence is not None and evidence.provider == "direct"
    assert "midias-ia/evidencias/h-pf.html" in memory.files
    assert unlocker.calls == []
    assert "enriquecimento: URLs" in out
    assert "evidências reaproveitadas" in out
    assert "evidências Bright Data" in out


def test_modo_brightdata_usa_so_o_unlocker(db: Path, capsys: pytest.CaptureFixture[str]):
    memory, unlocker = Memory(), Unlocker()

    code = enrich(
        db,
        "--evidence-mode",
        "brightdata",
        articles=Articles(),
        analyzer=Analyzer(),
        evidence_store=memory,
        bright_data=unlocker,
    )

    assert code == 0
    evidence = StateStore(db, read_only=True).get_evidence("h-pf")
    assert evidence is not None and evidence.provider == "brightdata"
    assert evidence.rejected_reasons == ("reuse_disabled",)
    assert len(unlocker.calls) == 1
    assert "Bright Data (chamadas)" in capsys.readouterr().out


def test_sem_chave_do_openrouter_aborta_antes_de_criar_execucao(
    db: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    code = main(["enrich", "--db", str(db)], now=NOW, enrich_ports=EnrichPorts(articles=Articles()))

    assert code == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
    assert StateStore(db, read_only=True).enrich_runs() == []


def test_modo_brightdata_sem_credenciais_aborta(
    db: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("BRIGHTDATA_API_KEY", raising=False)
    monkeypatch.delenv("BRIGHTDATA_ZONE_UNLOCKER", raising=False)

    code = main(
        ["enrich", "--db", str(db), "--evidence-mode", "brightdata"],
        now=NOW,
        enrich_ports=EnrichPorts(articles=Articles(), analyzer=Analyzer(), evidence_store=Memory()),
    )

    assert code == 2
    assert "BRIGHTDATA_API_KEY" in capsys.readouterr().err


def test_modo_auto_sem_credenciais_do_bright_data_avisa_e_segue(
    db: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("BRIGHTDATA_API_KEY", raising=False)
    monkeypatch.delenv("BRIGHTDATA_ZONE_UNLOCKER", raising=False)

    code = main(
        ["enrich", "--db", str(db)],
        now=NOW,
        enrich_ports=EnrichPorts(articles=Articles(), analyzer=Analyzer(), evidence_store=Memory()),
    )

    assert code == 0
    assert (
        "sem Bright Data" in capsys.readouterr().err
    )  # aviso, e o HTML confiável foi reaproveitado
    assert StateStore(db, read_only=True).get_evidence("h-pf").provider == "direct"  # type: ignore[union-attr]


def test_sem_estado_aborta_com_mensagem(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    code = main(
        ["enrich", "--db", str(tmp_path / "nao-existe.db")],
        now=NOW,
        enrich_ports=EnrichPorts(articles=Articles(), analyzer=Analyzer(), evidence_store=Memory()),
    )

    assert code == 2
    assert "estado" in capsys.readouterr().err


def test_limite_de_urls_por_execucao(db: Path, capsys: pytest.CaptureFixture[str]):
    store = StateStore(db)
    run = store.start_run(NOW)
    store.record_urls(
        [
            DiscoveredUrl(
                f"https://a.com/materia-{n}-fraude", f"h-{n}", "a.com", "s", None, None, None
            )
            for n in range(3)
        ],
        run_id=run,
        now=NOW,
        kind=UrlKind.NEW,
    )
    store.close()

    enrich(
        db,
        "--limit",
        "2",
        articles=Articles(),
        analyzer=Analyzer(),
        evidence_store=Memory(),
        bright_data=Unlocker(),
    )

    assert (
        len(StateStore(db, read_only=True).pending_enrichment()) == 2
    )  # 4 pendentes, 2 processadas


def test_parser_do_enrich_escolhe_o_armazenamento_pelo_ambiente_por_padrao():
    assert build_parser().parse_args(["enrich"]).evidence_store == "auto"
    assert build_parser().parse_args(["enrich", "--evidence-store", "s3"]).evidence_store == "s3"


def test_s3_sem_bucket_aborta_com_mensagem_sem_criar_execucao(
    db: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("AWS_BUCKET", raising=False)

    code = main(
        ["enrich", "--db", str(db), "--evidence-store", "s3"],
        now=NOW,
        enrich_ports=EnrichPorts(articles=Articles(), analyzer=Analyzer(), bright_data=Unlocker()),
    )

    assert code == 2
    assert "AWS_BUCKET" in capsys.readouterr().err
    assert StateStore(db, read_only=True).enrich_runs() == []


def test_disco_s3_do_ambiente_grava_a_evidencia_no_bucket(
    db: Path, monkeypatch: pytest.MonkeyPatch
):
    saved: dict[tuple[str, str], bytes] = {}

    class FakeClient:
        def put_object(self, **kwargs):
            saved[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

    monkeypatch.setattr(
        "buscador_midias.evidence.s3_storage._boto_client", lambda **kwargs: FakeClient()
    )
    monkeypatch.setenv("MIDIAS_IA_STORAGE_DISK", "s3")
    monkeypatch.setenv("AWS_BUCKET", "bucket-do-ambiente")

    code = main(
        ["enrich", "--db", str(db)],
        now=NOW,
        enrich_ports=EnrichPorts(articles=Articles(), analyzer=Analyzer(), bright_data=Unlocker()),
    )

    assert code == 0
    assert ("bucket-do-ambiente", "midias-ia/evidencias/h-pf.html") in saved
    evidence = StateStore(db, read_only=True).get_evidence("h-pf")
    assert evidence is not None and evidence.path == "midias-ia/evidencias/h-pf.html"


def test_local_continua_o_padrao_sem_variaveis(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MIDIAS_IA_STORAGE_DISK", raising=False)
    folder = tmp_path / "evidencias"

    code = main(
        ["enrich", "--db", str(db), "--evidence-dir", str(folder)],
        now=NOW,
        enrich_ports=EnrichPorts(articles=Articles(), analyzer=Analyzer(), bright_data=Unlocker()),
    )

    assert code == 0
    assert (folder / "midias-ia" / "evidencias" / "h-pf.html").is_file()
