from datetime import UTC, datetime, timedelta

from buscador_midias.discovery.stats import build_report, format_report
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import SiteRunRecord, StateStore, UrlKind

T0 = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=8)


def record(site: str, status: str = "ok", **counts: int) -> SiteRunRecord:
    base = {
        "new_urls": 0,
        "baseline_urls": 0,
        "backlog_urls": 0,
        "seen_urls": 0,
        "sitemaps_fetched": 0,
        "sitemaps_skipped": 0,
        "known_urls": 0,
        "sitemaps_failed": 0,
        "filtered_urls": 0,
    }
    base.update(counts)
    return SiteRunRecord(site_key=site, status=status, detail="", **base)


def url(path: str, published: datetime | None) -> DiscoveredUrl:
    return DiscoveredUrl(
        url=f"https://a.com/{path}",
        url_hash=f"h-{path}",
        site_key="a.com",
        source_sitemap="https://a.com/s.xml",
        lastmod=None,
        title=None,
        publication_date=published,
    )


def seeded_store() -> StateStore:
    store = StateStore()
    run1 = store.start_run(T0)
    store.record_site_run(run1, record("a.com", baseline_urls=5, seen_urls=5, sitemaps_fetched=1))
    store.record_site_run(run1, record("b.com", baseline_urls=3, seen_urls=3, sitemaps_fetched=1))
    store.record_site_run(run1, record("c.com", status="blocked"))
    store.finish_run(run1, T0 + timedelta(minutes=4))

    run2 = store.start_run(T1)
    store.record_site_run(
        run2,
        record(
            "a.com",
            new_urls=2,
            known_urls=1,
            seen_urls=7,
            sitemaps_fetched=2,
            sitemaps_skipped=1,
        ),
    )
    store.record_site_run(run2, record("b.com", seen_urls=3, sitemaps_fetched=1))
    store.record_site_run(run2, record("c.com", status="blocked"))
    store.finish_run(run2, T1 + timedelta(minutes=2))
    store.record_urls(
        [url("n1", T1 - timedelta(hours=2)), url("n2", T1 - timedelta(hours=6))],
        run_id=run2,
        now=T1,
        kind=UrlKind.NEW,
    )
    store.add_fallback_hosts(["c.com"], T0)
    return store


def test_resumo_por_rodada_soma_os_sites():
    report = build_report(seeded_store())

    first, second = report.runs
    assert (first.run_id, first.sites, first.ok, first.failed) == (1, 3, 2, 1)
    assert first.baseline == 8
    assert first.new == 0
    assert (second.run_id, second.new, second.known, second.seen) == (2, 2, 1, 10)
    assert (second.sitemaps_fetched, second.sitemaps_skipped) == (3, 1)
    assert second.started_at == T1
    assert second.finished_at == T1 + timedelta(minutes=2)


def test_sites_sem_novidade_exigem_ao_menos_uma_rodada_alem_da_linha_de_base():
    report = build_report(seeded_store())

    assert report.sites_without_new == ("b.com",)


def test_site_so_com_linha_de_base_ainda_nao_pode_ser_julgado():
    store = StateStore()
    run = store.start_run(T0)
    store.record_site_run(run, record("a.com", baseline_urls=4, seen_urls=4))

    assert build_report(store).sites_without_new == ()


def test_latencia_de_publicacao_so_das_novas_com_data():
    report = build_report(seeded_store())

    assert report.latency is not None
    assert report.latency.count == 2
    assert report.latency.median_hours == 4.0
    assert report.latency.p90_hours == 6.0


def test_sem_urls_novas_com_data_nao_ha_latencia():
    store = StateStore()
    store.start_run(T0)

    assert build_report(store).latency is None


def test_hosts_em_fallback_aparecem_no_relatorio():
    assert build_report(seeded_store()).fallback_hosts == ("c.com",)


def test_texto_do_relatorio_traz_as_rodadas_e_os_alertas():
    text = format_report(build_report(seeded_store()))

    assert "rodada 1" in text
    assert "rodada 2" in text
    assert "b.com" in text
    assert "c.com" in text
    assert "4.0" in text


def test_rodada_destaca_sites_ok_que_nao_leram_todos_os_sitemaps():
    store = StateStore()
    run = store.start_run(T0)
    store.record_site_run(run, record("a.com", sitemaps_fetched=3, sitemaps_failed=1))
    store.record_site_run(run, record("b.com", sitemaps_fetched=2))
    store.record_site_run(run, record("c.com", status="blocked", sitemaps_failed=2))
    store.finish_run(run, T0 + timedelta(minutes=1))

    report = build_report(store)

    assert report.runs[0].partial_sites == 1  # só site ok com falha parcial; blocked já é falha
    assert report.runs[0].sitemaps_failed == 1
    assert "leitura parcial 1" in format_report(report)


def test_resumo_traz_o_que_o_l0_descartou_e_por_que():
    store = StateStore()
    run = store.start_run(T0)
    store.record_site_run(run, record("a.com", new_urls=2, filtered_urls=7))
    store.record_site_run(run, record("b.com", filtered_urls=3))
    store.record_urls(
        [url("t1", None), url("t2", None)],
        run_id=run,
        now=T0,
        kind=UrlKind.FILTERED,
        reasons={"h-t1": "listing", "h-t2": "listing"},
    )
    store.finish_run(run, T0 + timedelta(minutes=1))

    report = build_report(store)

    assert report.runs[0].filtered == 10
    assert report.filter_reasons == {"listing": 2}
    text = format_report(report)
    assert "filtradas 10" in text
    assert "listing 2" in text


def test_relatorio_traz_execucoes_da_triagem_e_o_custo_acumulado():
    from buscador_midias.state.store import TriageRecord, TriageRunTotals

    store = seeded_store()
    first = store.start_triage_run(T1, model="google/x", prompt_version="v1")
    store.record_triage(
        [
            TriageRecord("h-n1", "keep", ("fiscal",), "fraude"),
            TriageRecord("h-n2", "discard", (), "clima"),
        ],
        triage_run_id=first,
        now=T1,
    )
    store.finish_triage_run(
        first,
        T1,
        TriageRunTotals(
            headlines=2,
            kept=1,
            discarded=1,
            prompt_tokens=900,
            completion_tokens=80,
            cost_usd=0.0004,
        ),
    )
    second = store.start_triage_run(T1, model="google/x", prompt_version="v1")
    store.finish_triage_run(second, T1, TriageRunTotals(headlines=1, pending=1, cost_usd=0.0001))

    report = build_report(store)
    text = format_report(report)

    assert [r.id for r in report.triage_runs] == [first, second]
    assert report.triage_counts == {"keep": 1, "discard": 1}
    assert report.triage_cost_usd == 0.0005
    assert "triagem LLM" in text
    assert "google/x" in text
    assert "mantidas 1" in text
    assert "custo total US$ 0.0005" in text


def test_sem_triagem_o_relatorio_nao_menciona_custo():
    text = format_report(build_report(seeded_store()))

    assert "triagem LLM" not in text


def test_rodada_mostra_o_uso_do_bright_data():
    store = StateStore()
    run = store.start_run(T0)
    store.record_site_run(run, record("a.com"))
    store.finish_run(run, T0 + timedelta(minutes=1))
    store.record_run_usage(run, bright_data_requests=12, bright_data_hosts=2)
    quiet = store.start_run(T1)
    store.record_site_run(quiet, record("a.com"))
    store.finish_run(quiet, T1 + timedelta(minutes=1))

    report = build_report(store)
    text = format_report(report)

    assert [r.bright_data_requests for r in report.runs] == [12, 0]
    assert "bright data 12 req" in text
    assert text.count("bright data") == 1  # rodada sem uso não menciona


def test_relatorio_traz_execucoes_do_enriquecimento_e_o_acumulado():
    from buscador_midias.state.store import AnalysisRecord, EnrichTotals, EvidenceRecord

    store = seeded_store()
    run = store.start_enrich_run(
        T1, model="google/x", prompt_version="ctx-v2", evidence_mode="auto"
    )
    store.finish_enrich_run(
        run,
        T1,
        EnrichTotals(
            urls=80,
            fetched_ok=79,
            fetched_other=1,
            analyzed=80,
            findings=7,
            evidence_reused=6,
            evidence_brightdata=1,
            bright_data_requests=1,
            prompt_tokens=35000,
            completion_tokens=11000,
            cost_usd=0.0079,
        ),
    )
    store.record_analysis(
        AnalysisRecord("h-n1", True, (), (), "x", "y", 0.9, "text"),
        enrich_run_id=run,
        model="m",
        prompt_version="v",
        now=T1,
    )
    store.record_evidence(
        EvidenceRecord(
            "h-n1",
            "captured",
            "direct",
            "p",
            "ab" * 32,
            1,
            "text/html",
            200,
            None,
            ("thin_content",),
            None,
        ),
        now=T1,
    )

    report = build_report(store)
    text = format_report(report)

    assert [r.id for r in report.enrich_runs] == [run]
    assert report.enrich_cost_usd == 0.0079
    assert report.evidence_reasons == {"thin_content": 1}
    assert "enriquecimento #1 (google/x, ctx-v2, auto)" in text
    assert "achados adversos 7" in text
    assert "reaproveitadas 6" in text
    assert "Bright Data 1" in text
    assert "enriquecimento acumulado" in text
    assert "com entidade 1" in text  # o acumulado separa achados adversos de matérias com entidade
    assert "motivos de não reaproveitar o HTML" in text
    assert "thin_content 1" in text


def test_sem_enriquecimento_o_relatorio_nao_menciona_o_assunto():
    assert "enriquecimento" not in format_report(build_report(seeded_store()))


def test_relatorio_traz_as_gravacoes_no_corpus_e_o_acumulado():
    from buscador_midias.state.store import CorpusTotals

    store = seeded_store()
    run = store.start_corpus_run(T1)
    store.record_corpus_write(
        "h-n1", article_id=5, created=True, evidence_state="captured", corpus_run_id=run, now=T1
    )
    store.finish_corpus_run(
        run,
        T1,
        CorpusTotals(
            candidates=3,
            created=2,
            existing=1,
            evidence_updated=1,
            entities=5,
            keywords=4,
            failed=0,
            withheld=6,
            rewritten=1,
        ),
    )

    report = build_report(store)
    text = format_report(report)

    assert [r.id for r in report.corpus_runs] == [run]
    assert report.corpus_counts == {"written": 1, "created": 1}
    assert "corpus #1: candidatos 3 | criados 2, já existiam 1" in text
    assert "evidências atualizadas 1" in text
    assert "retidos 6" in text
    assert "corpus acumulado: gravados 1, criados por nós 1" in text


def test_sem_gravacao_o_relatorio_nao_menciona_o_corpus():
    text = format_report(build_report(seeded_store()))

    assert "corpus #" not in text
    assert "corpus acumulado" not in text
