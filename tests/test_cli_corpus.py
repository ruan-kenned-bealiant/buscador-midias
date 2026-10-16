from datetime import UTC, datetime

import pytest

from buscador_midias.cli import build_parser, main
from buscador_midias.corpus.article import CorpusArticle
from buscador_midias.corpus.writer import CorpusSchemaError, WriteOutcome
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import (
    AnalysisRecord,
    ContentRecord,
    EvidenceRecord,
    StateStore,
    UrlKind,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class FakeWriter:
    def __init__(self, schema_error: bool = False) -> None:
        self.corpus: dict[str, CorpusArticle] = {}
        self.schema_error = schema_error
        self.closed = False

    def check_schema(self) -> None:
        if self.schema_error:
            raise CorpusSchemaError("o corpus não tem: midias_ia_articles.ai_summary")

    def write(self, articles):
        out = []
        for article in articles:
            self.corpus[article.url_hash] = article
            out.append(
                WriteOutcome(
                    article.url_hash,
                    len(self.corpus),
                    True,
                    False,
                    len(article.entities),
                    len(article.keywords),
                )
            )
        return out

    def existing(self, hashes):
        return {h for h in hashes if h in self.corpus}

    def source_names(self):
        return {}

    def close(self) -> None:
        self.closed = True


def make_db(basis: str = "text") -> None:
    store = StateStore()
    run = store.start_run(NOW)
    enrich = store.start_enrich_run(NOW, model="m", prompt_version="v", evidence_mode="auto")
    store.record_urls(
        [DiscoveredUrl("https://a.com/x-1", "h-1", "a.com", "s", None, "T", None)],
        run_id=run,
        now=NOW,
        kind=UrlKind.NEW,
    )
    store.record_content(
        ContentRecord(
            "h-1", "ok", 200, None, "Manchete", "D", "Texto " * 50, 300, NOW, "pt-BR", None
        ),
        now=NOW,
    )
    store.record_analysis(
        AnalysisRecord(
            "h-1",
            True,
            ("criminal",),
            ({"name": "João Silva", "type": "PF", "role": "acusado"},),
            "fato",
            "r",
            0.9,
            basis,
        ),
        enrich_run_id=enrich,
        model="m",
        prompt_version="v",
        now=NOW,
    )
    store.record_evidence(
        EvidenceRecord(
            "h-1",
            "captured",
            "direct",
            "midias-ia/evidencias/h-1.html",
            "ab" * 32,
            1,
            "text/html",
            200,
            None,
            (),
            None,
        ),
        now=NOW,
    )
    store.close()


def corpus_sync(*extra: str, writer=None):
    return main(["corpus-sync", *extra], now=NOW, corpus_writer=writer)


def test_parser_tem_padroes():
    args = build_parser().parse_args(["corpus-sync"])

    assert args.batch_size == 50
    assert args.dry_run is False


def test_grava_os_achados_e_imprime_o_resumo(capsys: pytest.CaptureFixture[str]):
    make_db()
    writer = FakeWriter()

    code = corpus_sync(writer=writer)

    out = capsys.readouterr().out
    assert code == 0
    assert set(writer.corpus) == {"h-1"}
    assert "corpus: candidatos" in out and "criados" in out
    assert writer.closed is True
    assert StateStore(read_only=True).corpus_write_counts() == {"written": 1, "created": 1}


def test_dry_run_so_conta(capsys: pytest.CaptureFixture[str]):
    make_db()
    writer = FakeWriter()

    assert corpus_sync("--dry-run", writer=writer) == 0

    assert writer.corpus == {}
    assert "simulação" in capsys.readouterr().out
    assert StateStore(read_only=True).corpus_write_counts() == {"written": 0, "created": 0}


def test_analise_so_pela_manchete_nao_atende_ao_criterio_e_nao_entra():
    make_db(basis="headline")
    writer = FakeWriter()

    assert corpus_sync(writer=writer) == 0

    assert writer.corpus == {}


def test_sem_variaveis_do_corpus_aborta_sem_vazar_senha(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    make_db()
    for name in (
        "DB_MIDIAS_IA_HOST",
        "DB_MIDIAS_IA_PORT",
        "DB_MIDIAS_IA_DATABASE",
        "DB_MIDIAS_IA_USERNAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_MIDIAS_IA_PASSWORD", "segredo-123")

    code = corpus_sync()

    err = capsys.readouterr().err
    assert code == 2
    assert "DB_MIDIAS_IA_HOST" in err and "segredo-123" not in err


def test_esquema_incompativel_aborta_antes_de_gravar(capsys: pytest.CaptureFixture[str]):
    make_db()
    writer = FakeWriter(schema_error=True)

    code = corpus_sync(writer=writer)

    assert code == 2
    assert "ai_summary" in capsys.readouterr().err
    assert writer.corpus == {} and writer.closed is True
    assert StateStore(read_only=True).corpus_runs() == []


def test_falha_de_conexao_aborta_com_o_tipo_do_erro(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    make_db()
    for name, value in (
        ("DB_MIDIAS_IA_HOST", "127.0.0.1"),
        ("DB_MIDIAS_IA_PORT", "1"),
        ("DB_MIDIAS_IA_DATABASE", "x"),
        ("DB_MIDIAS_IA_USERNAME", "u"),
        ("DB_MIDIAS_IA_PASSWORD", "segredo-123"),
    ):
        monkeypatch.setenv(name, value)

    code = corpus_sync()

    err = capsys.readouterr().err
    assert code == 2
    assert "corpus" in err and "segredo-123" not in err
