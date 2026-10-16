from datetime import UTC, datetime, timedelta

from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import (
    AnalysisRecord,
    ContentRecord,
    EnrichTotals,
    EvidenceRecord,
    StateStore,
    UrlKind,
)

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def found(path: str, *, title: str | None = None) -> DiscoveredUrl:
    return DiscoveredUrl(
        url=f"https://a.com/{path}",
        url_hash=f"h-{path}",
        site_key="a.com",
        source_sitemap="https://a.com/s.xml",
        lastmod=None,
        title=title,
        publication_date=None,
    )


def seeded(*paths: str, other: tuple[str, ...] = ()) -> StateStore:
    """URLs ``new`` e, em ``other``, URLs de linha de base (que nunca entram na análise)."""
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found(p) for p in paths], run_id=run, now=T0, kind=UrlKind.NEW)
    store.record_urls([found(p) for p in other], run_id=run, now=T0, kind=UrlKind.BASELINE)
    return store


def content(path: str, status: str = "ok", **kw) -> ContentRecord:
    base = {
        "url_hash": f"h-{path}",
        "status": status,
        "http_status": 200,
        "final_url": f"https://a.com/{path}",
        "title": "Titulo",
        "description": "Desc",
        "lede": "texto do artigo",
        "text_chars": 1200,
        "published_at": T0,
        "language": "pt-BR",
        "error": None,
    }
    base.update(kw)
    return ContentRecord(**base)


def analysis(path: str, *, finding: bool = True, **kw) -> AnalysisRecord:
    base = {
        "url_hash": f"h-{path}",
        "finding": finding,
        "themes": ("fiscal",),
        "entities": ({"name": "João Silva", "type": "PF", "role": "acusado"},),
        "adverse_fact": "preso por fraude",
        "summary": "Resumo curto",
        "confidence": 0.9,
        "basis": "text",
    }
    base.update(kw)
    return AnalysisRecord(**base)


def test_so_urls_novas_e_sem_analise_estao_pendentes():
    store = seeded("a-1", "b-2", other=("c-3",))
    run = store.start_enrich_run(T0, model="m", prompt_version="v", evidence_mode="auto")
    store.record_content(content("a-1"), now=T0)
    store.record_analysis(analysis("a-1"), enrich_run_id=run, model="m", prompt_version="v", now=T0)

    pending = store.pending_enrichment()

    assert [p.url_hash for p in pending] == ["h-b-2"]


def test_fila_de_analise_intercala_as_fontes_para_o_intervalo_por_host_nao_travar_o_bloco():
    store = StateStore()
    run = store.start_run(T0)

    def on(site: str, n: int) -> DiscoveredUrl:
        return DiscoveredUrl(f"https://{site}/m-{n}", f"h-{site}-{n}", site, "s", None, None, None)

    urls = [on("a.com", n) for n in range(3)] + [on("b.com", n) for n in range(2)]
    store.record_urls(urls, run_id=run, now=T0, kind=UrlKind.NEW)

    order = [p.url_hash for p in store.pending_enrichment()]

    assert order == ["h-a.com-0", "h-b.com-0", "h-a.com-1", "h-b.com-1", "h-a.com-2"]
    assert [p.url_hash for p in store.pending_enrichment(limit=3)] == order[:3]


def test_falha_de_download_conta_tentativas_e_segue_pendente_ate_ter_analise():
    store = seeded("a-1")

    for expected in (1, 2, 3):
        store.record_content(
            content("a-1", status="error", http_status=None, error="Timeout"), now=T0
        )
        assert store.fetch_attempts("h-a-1") == expected

    # quem decide desistir do download (e analisar só pela manchete) é o pipeline
    assert [p.url_hash for p in store.pending_enrichment()] == ["h-a-1"]
    assert store.fetch_attempts("h-sem-registro") == 0


def test_download_bloqueado_nao_e_tentado_de_novo_pelo_pendente():
    store = seeded("a-1")
    store.record_content(content("a-1", status="blocked", http_status=403), now=T0)

    # sem análise ainda: segue pendente para analisar só pela manchete, mas sem novo download
    [pending] = store.pending_enrichment()
    assert pending.content_status == "blocked"


def test_conteudo_e_gravado_com_texto_data_e_idioma():
    store = seeded("a-1")

    store.record_content(content("a-1"), now=T0)

    saved = store.get_content("h-a-1")
    assert saved is not None
    assert (saved.title, saved.lede, saved.language, saved.published_at) == (
        "Titulo",
        "texto do artigo",
        "pt-BR",
        T0,
    )
    assert store.get_content("h-nao-existe") is None


def test_analise_guarda_entidades_temas_e_a_base_usada():
    store = seeded("a-1")
    run = store.start_enrich_run(T0, model="google/x", prompt_version="v9", evidence_mode="auto")

    store.record_analysis(
        analysis("a-1"), enrich_run_id=run, model="google/x", prompt_version="v9", now=T0
    )

    saved = store.get_analysis("h-a-1")
    assert saved is not None
    assert saved.finding is True
    assert saved.themes == ("fiscal",)
    assert saved.entities[0]["name"] == "João Silva"
    assert saved.basis == "text"
    assert (saved.model, saved.prompt_version) == ("google/x", "v9")


def test_evidencia_pendente_so_para_materias_com_entidade_sem_evidencia_capturada():
    store = seeded("a-1", "b-2", "c-3")
    run = store.start_enrich_run(T0, model="m", prompt_version="v", evidence_mode="auto")
    for path, finding in (("a-1", True), ("b-2", False), ("c-3", True)):
        store.record_analysis(
            analysis(path, finding=finding, **({} if finding else {"entities": ()})),
            enrich_run_id=run,
            model="m",
            prompt_version="v",
            now=T0,
        )
    store.record_evidence(
        EvidenceRecord(
            url_hash="h-c-3",
            status="captured",
            provider="direct",
            path="midias-ia/evidencias/h-c-3.html",
            sha256="ab" * 32,
            size_bytes=1234,
            content_type="text/html",
            http_status=200,
            final_url="https://a.com/c-3",
            rejected_reasons=(),
            error=None,
        ),
        now=T0,
    )

    assert [p.url_hash for p in store.pending_evidence()] == ["h-a-1"]


def test_evidencia_com_falha_e_retentada_ate_tres_vezes():
    store = seeded("a-1")
    run = store.start_enrich_run(T0, model="m", prompt_version="v", evidence_mode="auto")
    store.record_analysis(analysis("a-1"), enrich_run_id=run, model="m", prompt_version="v", now=T0)
    failed = EvidenceRecord(
        "h-a-1", "failed", None, None, None, 0, None, 200, None, ("paywall",), "x"
    )

    for expected in (1, 2, 3):
        store.record_evidence(failed, now=T0)
        assert store.get_evidence("h-a-1").attempts == expected  # type: ignore[union-attr]

    assert store.pending_evidence(max_attempts=3) == []


def test_evidencia_guarda_o_motivo_da_rejeicao_do_reaproveitamento():
    store = seeded("a-1")
    record = EvidenceRecord(
        "h-a-1",
        "captured",
        "brightdata",
        "p/h-a-1.html",
        "cd" * 32,
        10,
        "text/html",
        200,
        None,
        ("thin_content", "paywall"),
        None,
    )

    store.record_evidence(record, now=T0)

    saved = store.get_evidence("h-a-1")
    assert saved is not None
    assert saved.provider == "brightdata"
    assert saved.rejected_reasons == ("thin_content", "paywall")
    assert saved.sha256 == "cd" * 32


def test_execucao_de_enriquecimento_guarda_totais_e_custos():
    store = StateStore()
    run = store.start_enrich_run(T0, model="google/x", prompt_version="v1", evidence_mode="auto")
    totals = EnrichTotals(
        urls=10,
        fetched_ok=7,
        fetched_blocked=2,
        fetched_other=1,
        analyzed=10,
        findings=3,
        evidence_reused=2,
        evidence_brightdata=1,
        evidence_failed=0,
        bright_data_requests=1,
        prompt_tokens=5000,
        completion_tokens=900,
        cost_usd=0.002,
        retries=1,
        errors=0,
    )

    store.finish_enrich_run(run, T0 + timedelta(minutes=3), totals)

    [saved] = store.enrich_runs()
    assert (saved.id, saved.model, saved.prompt_version, saved.evidence_mode) == (
        run,
        "google/x",
        "v1",
        "auto",
    )
    assert saved.totals == totals
    assert saved.finished_at == T0 + timedelta(minutes=3)


def test_contagens_para_o_painel():
    store = seeded("a-1", "b-2", "c-3")
    run = store.start_enrich_run(T0, model="m", prompt_version="v", evidence_mode="auto")
    store.record_content(content("a-1"), now=T0)
    store.record_content(content("b-2", status="blocked", http_status=403), now=T0)
    store.record_analysis(analysis("a-1"), enrich_run_id=run, model="m", prompt_version="v", now=T0)
    store.record_analysis(
        analysis("b-2", finding=False, basis="headline"),
        enrich_run_id=run,
        model="m",
        prompt_version="v",
        now=T0,
    )
    store.record_evidence(
        EvidenceRecord(
            "h-a-1", "captured", "direct", "p", "ab" * 32, 10, "text/html", 200, None, (), None
        ),
        now=T0,
    )

    counts = store.enrichment_counts()

    assert counts["content"] == {"ok": 1, "blocked": 1}
    assert counts["analysis"] == {
        "findings": 1,
        "no_finding": 1,
        "headline_only": 1,
        "relevant": 1,  # a análise só pela manchete não atende ao critério
    }
    assert counts["evidence"] == {"captured:direct": 1}
    assert counts["pending_enrichment"] == 1  # c-3 ainda sem análise
    assert counts["pending_evidence"] == 0


def test_evidencia_exige_so_as_entidades_com_papel_adverso():
    store = seeded("a-1")
    run = store.start_enrich_run(T0, model="m", prompt_version="v", evidence_mode="auto")
    entities = (
        {"name": "Polícia Civil", "type": "OUTRO", "role": "autoridade"},
        {"name": "João Silva", "type": "PF", "role": "acusado"},
        {"name": "Maria Souza", "type": "PF", "role": "vitima"},
        {"name": "Empresa X", "type": "PJ", "role": "responsavel"},
    )
    store.record_analysis(
        analysis("a-1", entities=entities), enrich_run_id=run, model="m", prompt_version="v", now=T0
    )

    [finding] = store.pending_evidence()

    assert finding.entity_names == ("João Silva", "Empresa X")


def test_motivos_de_rejeicao_do_reaproveitamento_somados_por_codigo():
    store = seeded("a-1", "b-2", "c-3")
    for path, reasons in (
        ("a-1", ("thin_content", "paywall")),
        ("b-2", ("thin_content",)),
        ("c-3", ()),
    ):
        store.record_evidence(
            EvidenceRecord(
                f"h-{path}",
                "captured",
                "brightdata" if reasons else "direct",
                "p",
                "ab" * 32,
                1,
                "text/html",
                200,
                None,
                reasons,
                None,
            ),
            now=T0,
        )

    assert store.evidence_rejection_reasons() == {"thin_content": 2, "paywall": 1}


def test_ultimos_achados_trazem_entidades_adversas_e_estado_da_evidencia():
    store = seeded("a-1", "b-2", "c-3")
    run = store.start_enrich_run(T0, model="m", prompt_version="v", evidence_mode="auto")
    entities = (
        {"name": "Polícia Civil", "type": "OUTRO", "role": "autoridade"},
        {"name": "João Silva", "type": "PF", "role": "acusado"},
    )
    store.record_analysis(
        analysis("a-1", entities=entities), enrich_run_id=run, model="m", prompt_version="v", now=T0
    )
    store.record_analysis(
        analysis("b-2", finding=False), enrich_run_id=run, model="m", prompt_version="v", now=T0
    )
    store.record_analysis(
        analysis("c-3"),
        enrich_run_id=run,
        model="m",
        prompt_version="v",
        now=T0 + timedelta(minutes=5),
    )
    store.record_evidence(
        EvidenceRecord(
            "h-a-1",
            "captured",
            "direct",
            "midias-ia/evidencias/h-a-1.html",
            "ab" * 32,
            10,
            "text/html",
            200,
            None,
            (),
            None,
        ),
        now=T0,
    )

    findings = store.recent_findings(limit=10)

    assert [f.url for f in findings] == [
        "https://a.com/c-3",
        "https://a.com/a-1",
    ]  # mais recente primeiro
    newest, older = findings
    assert older.adverse_entities == ("João Silva",)
    assert older.evidence_status == "captured" and older.evidence_provider == "direct"
    assert older.themes == ("fiscal",)
    assert (newest.evidence_status, newest.evidence_provider) == (None, None)
    assert len(store.recent_findings(limit=1)) == 1


def record(store: StateStore, path: str, **kw) -> None:
    run = store.start_enrich_run(T0, model="m", prompt_version="v", evidence_mode="auto")
    store.record_analysis(
        analysis(path, **kw), enrich_run_id=run, model="m", prompt_version="v", now=T0
    )


def test_materia_neutra_com_entidade_tambem_espera_evidencia_e_nao_e_adversa():
    store = seeded("a-1", "b-2")
    neutral = ({"name": "Vinícius Júnior", "type": "PF", "role": "outro"},)
    record(store, "a-1")
    record(store, "b-2", finding=False, themes=(), entities=neutral, adverse_fact="")

    pending = {p.url_hash: p for p in store.pending_evidence()}

    assert set(pending) == {"h-a-1", "h-b-2"}
    assert pending["h-a-1"].adverse is True
    assert pending["h-b-2"].adverse is False
    assert pending["h-b-2"].entity_names == ("Vinícius Júnior",)  # neutra: qualquer entidade vale
    assert store.count_pending_evidence() == 2


def test_so_manchete_ou_sem_entidade_nao_espera_evidencia():
    store = seeded("a-1", "b-2", "c-3", "d-4")
    neutral = ({"name": "Empresa X", "type": "PJ", "role": "outro"},)
    record(store, "a-1", finding=False, themes=(), entities=neutral, basis="headline")
    record(store, "b-2", finding=False, themes=(), entities=())
    record(store, "c-3", basis="headline")  # achado só pela manchete: não atende ao critério
    record(store, "d-4", finding=False, themes=(), entities=neutral)

    assert {p.url_hash for p in store.pending_evidence()} == {"h-d-4"}
    assert store.count_pending_evidence() == 1


def test_contagem_do_painel_separa_achados_adversos_das_materias_com_entidade():
    store = seeded("a-1", "b-2", "c-3")
    neutral = ({"name": "Empresa X", "type": "PJ", "role": "outro"},)
    record(store, "a-1")
    record(store, "b-2", finding=False, themes=(), entities=neutral)
    record(store, "c-3", finding=False, themes=(), entities=())

    analysis_counts = store.enrichment_counts()["analysis"]

    assert analysis_counts["findings"] == 1
    assert analysis_counts["relevant"] == 2  # o achado também tem entidade
