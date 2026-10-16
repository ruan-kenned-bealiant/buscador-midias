from datetime import UTC, datetime

from buscador_midias.corpus.article import CorpusArticle
from buscador_midias.corpus.sync import sync_corpus
from buscador_midias.corpus.writer import WriteOutcome
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.domain.site import Site
from buscador_midias.state.store import (
    AnalysisRecord,
    ContentRecord,
    EvidenceRecord,
    StateStore,
    UrlKind,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def found(path: str) -> DiscoveredUrl:
    return DiscoveredUrl(
        f"https://a.com/{path}", f"h-{path}", "a.com", "s", None, f"Titulo {path}", None
    )


def site() -> Site:
    return Site.create(
        name="A",
        url="https://a.com",
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


class FakeWriter:
    def __init__(self) -> None:
        self.corpus: dict[str, CorpusArticle] = {}
        self.calls: list[list[str]] = []
        self.fail: set[str] = set()
        self.names = {"a.com": "Fonte A"}

    def write(self, articles):
        self.calls.append([a.url_hash for a in articles])
        outcomes = []
        for article in articles:
            if article.url_hash in self.fail:
                outcomes.append(
                    WriteOutcome(article.url_hash, None, False, False, 0, 0, "DataError")
                )
                continue
            created = article.url_hash not in self.corpus
            self.corpus[article.url_hash] = article
            outcomes.append(
                WriteOutcome(
                    article.url_hash,
                    len(self.corpus),
                    created,
                    not created,
                    len(article.entities),
                    len(article.keywords),
                )
            )
        return outcomes

    def existing(self, url_hashes):
        return {h for h in url_hashes if h in self.corpus}

    def source_names(self):
        return self.names


def seed(
    *paths: str, basis: str = "text", confidence: float = 0.9, evidence: str | None = "captured"
) -> StateStore:
    store = StateStore()
    run = store.start_run(NOW)
    enrich = store.start_enrich_run(NOW, model="m", prompt_version="v", evidence_mode="auto")
    store.record_urls([found(p) for p in paths], run_id=run, now=NOW, kind=UrlKind.NEW)
    for p in paths:
        store.record_content(
            ContentRecord(
                f"h-{p}",
                "ok",
                200,
                None,
                f"Manchete {p}",
                "Desc",
                "Texto " * 50,
                900,
                NOW,
                "pt-BR",
                None,
            ),
            now=NOW,
        )
        store.record_analysis(
            AnalysisRecord(
                f"h-{p}",
                True,
                ("criminal",),
                ({"name": "João Silva", "type": "PF", "role": "acusado"},),
                "fato",
                "resumo",
                confidence,
                basis,
            ),
            enrich_run_id=enrich,
            model="m",
            prompt_version="v",
            now=NOW,
        )
        if evidence:
            store.record_evidence(
                EvidenceRecord(
                    f"h-{p}",
                    evidence,
                    "direct" if evidence == "captured" else None,
                    f"midias-ia/evidencias/h-{p}.html" if evidence == "captured" else None,
                    "ab" * 32 if evidence == "captured" else None,
                    1,
                    "text/html",
                    200,
                    None,
                    (),
                    None,
                ),
                now=NOW,
            )
    return store


def run(store, writer, **kw):
    kw.setdefault("sites", {"a.com": site()})
    return sync_corpus(store, writer, now=NOW, **kw)


def test_grava_os_achados_pendentes_e_registra_cada_gravacao():
    store = seed("a-1", "b-2")
    writer = FakeWriter()

    summary = run(store, writer)

    assert set(writer.corpus) == {"h-a-1", "h-b-2"}
    totals = summary.totals
    assert (totals.candidates, totals.created, totals.existing, totals.failed) == (2, 2, 0, 0)
    assert (totals.entities, totals.keywords) == (2, 2)
    assert store.corpus_write_counts() == {"written": 2, "created": 2}
    assert store.pending_corpus_sync() == []


def test_o_artigo_gravado_leva_nome_da_fonte_pais_e_texto():
    store = seed("a-1")
    writer = FakeWriter()

    run(store, writer)

    article = writer.corpus["h-a-1"]
    assert (article.source_name, article.source_country, article.domain) == (
        "Fonte A",
        "BR",
        "a.com",
    )
    assert article.title == "Manchete a-1" and article.body.startswith("Texto")
    assert article.evidence.status == "captured"


def test_segunda_execucao_sem_novidade_nao_grava_nada():
    store = seed("a-1")
    writer = FakeWriter()
    run(store, writer)
    writer.calls.clear()

    summary = run(store, writer)

    assert writer.calls == []
    assert summary.totals.candidates == 0


def test_mudanca_no_estado_da_evidencia_regrava_o_artigo():
    store = seed("a-1", evidence="failed")
    writer = FakeWriter()
    run(store, writer)
    assert writer.corpus["h-a-1"].evidence.status == "failed"
    store.record_evidence(
        EvidenceRecord(
            "h-a-1",
            "captured",
            "direct",
            "midias-ia/evidencias/h-a-1.html",
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

    summary = run(store, writer)

    assert writer.corpus["h-a-1"].evidence.status == "captured"
    assert summary.totals.candidates == 1 and summary.totals.created == 0
    assert summary.totals.evidence_updated == 1


def test_sem_nenhuma_tentativa_de_evidencia_o_artigo_vai_como_pendente():
    store = seed("a-1", evidence=None)
    writer = FakeWriter()

    run(store, writer)

    assert writer.corpus["h-a-1"].evidence.status == "pending"


def test_falha_na_gravacao_nao_registra_e_volta_na_proxima_execucao():
    store = seed("a-1", "b-2")
    writer = FakeWriter()
    writer.fail = {"h-a-1"}

    first = run(store, writer)

    assert (first.totals.created, first.totals.failed) == (1, 1)
    assert [c.url_hash for c in store.pending_corpus_sync()] == ["h-a-1"]
    writer.fail = set()
    second = run(store, writer)
    assert (second.totals.created, second.totals.failed) == (1, 0)


def test_analise_so_pela_manchete_nao_atende_ao_criterio_e_nunca_entra_no_corpus():
    store = seed("a-1", "b-2", basis="headline")
    writer = FakeWriter()

    summary = run(store, writer)

    assert writer.corpus == {}
    assert summary.totals.candidates == 0


def test_gravacao_que_sumiu_do_corpus_e_refeita():
    store = seed("a-1", "b-2")
    writer = FakeWriter()
    run(store, writer)
    del writer.corpus["h-a-1"]  # ex.: um teste do KyX apagou a linha

    summary = run(store, writer)

    assert "h-a-1" in writer.corpus
    assert summary.totals.rewritten == 1
    assert summary.totals.created == 1


def test_dry_run_nao_grava_nem_muda_o_estado():
    store = seed("a-1", "b-2")
    writer = FakeWriter()

    summary = run(store, writer, dry_run=True)

    assert writer.calls == [] and writer.corpus == {}
    assert summary.totals.candidates == 2
    assert store.corpus_write_counts() == {"written": 0, "created": 0}
    assert store.corpus_runs() == []


def test_lotes_respeitam_o_tamanho_e_o_limite():
    store = seed(*(f"p-{n}" for n in range(5)))
    writer = FakeWriter()

    summary = run(store, writer, batch_size=2, limit=4)

    assert [len(c) for c in writer.calls] == [2, 2]
    assert summary.totals.candidates == 4
    assert len(store.pending_corpus_sync()) == 1


def test_execucao_fica_registrada_com_os_totais():
    store = seed("a-1")

    summary = run(store, FakeWriter())

    [saved] = store.corpus_runs()
    assert saved.id == summary.run_id
    assert saved.totals == summary.totals
    assert saved.finished_at == NOW


def test_sem_pendentes_a_execucao_e_registrada_mesmo_assim():
    store = StateStore()

    summary = run(store, FakeWriter())

    assert summary.totals.candidates == 0
    assert len(store.corpus_runs()) == 1


def seed_neutral(path: str, *, basis: str = "text", entities: bool = True) -> StateStore:
    store = seed(path, basis=basis)
    run = store.start_enrich_run(NOW, model="m", prompt_version="v2", evidence_mode="auto")
    store._run("DELETE FROM midias_crawler_analyses WHERE url_hash = %s", (f"h-{path}",))  # noqa: SLF001
    store.record_analysis(
        AnalysisRecord(
            f"h-{path}",
            False,
            (),
            ({"name": "Vinícius Júnior", "type": "PF", "role": "outro"},) if entities else (),
            "",
            "resumo",
            0.9,
            basis,
        ),
        enrich_run_id=run,
        model="m",
        prompt_version="v2",
        now=NOW,
    )
    return store


def test_materia_neutra_com_entidade_vai_para_o_corpus_sem_ser_adversa():
    store = seed_neutral("a-1")
    writer = FakeWriter()

    summary = sync_corpus(store, writer, sites={"a.com": site()}, now=NOW)

    assert summary.totals.created == 1
    article = writer.corpus["h-a-1"]
    assert [e.name for e in article.entities] == ["Vinícius Júnior"]


def test_materia_sem_entidade_nunca_entra_no_corpus():
    store = seed_neutral("a-1", entities=False)
    writer = FakeWriter()

    summary = sync_corpus(store, writer, sites={"a.com": site()}, now=NOW)

    assert summary.totals.candidates == 0
    assert writer.corpus == {}


def test_neutra_so_da_manchete_tambem_nao_atende_ao_criterio():
    store = seed_neutral("a-1", basis="headline")
    writer = FakeWriter()

    summary = sync_corpus(store, writer, sites={"a.com": site()}, now=NOW)

    assert summary.totals.candidates == 0
    assert writer.corpus == {}
