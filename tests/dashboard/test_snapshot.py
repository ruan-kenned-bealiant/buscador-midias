import json
from datetime import UTC, datetime, timedelta

from buscador_midias.dashboard.snapshot import build_snapshot
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import SiteRunRecord, StateStore, UrlKind

NOW = datetime(2026, 10, 15, 12, 30, tzinfo=UTC)
T0 = NOW - timedelta(hours=3)


def found(path: str, *, site: str = "a.com", title: str | None = None) -> DiscoveredUrl:
    return DiscoveredUrl(
        url=f"https://{site}/{path}",
        url_hash=f"hash-{site}-{path}",
        site_key=site,
        source_sitemap=f"https://{site}/sitemap.xml",
        lastmod=None,
        title=title,
        publication_date=None,
    )


def site_run(site: str, status: str = "ok", **kw: int | str) -> SiteRunRecord:
    base: dict[str, int | str] = {
        "new_urls": 0,
        "baseline_urls": 0,
        "backlog_urls": 0,
        "seen_urls": 0,
        "sitemaps_fetched": 1,
        "sitemaps_skipped": 0,
        "detail": "",
    }
    base.update(kw)
    return SiteRunRecord(site_key=site, status=status, **base)  # type: ignore[arg-type]


def seeded() -> StateStore:
    store = StateStore()
    run1 = store.start_run(T0)
    store.record_site_run(run1, site_run("a.com", baseline_urls=5, seen_urls=5))
    store.record_site_run(run1, site_run("b.com", baseline_urls=3, seen_urls=3))
    store.record_site_run(run1, site_run("c.com", status="blocked", detail="HTTP 403"))
    store.finish_run(run1, T0 + timedelta(minutes=4))
    store.record_urls([found("base-1")], run_id=run1, now=T0, kind=UrlKind.BASELINE)

    run2 = store.start_run(NOW - timedelta(hours=1))
    store.record_site_run(run2, site_run("a.com", new_urls=2, seen_urls=7, filtered_urls=1))
    store.record_site_run(
        run2, site_run("b.com", sitemaps_failed=1, detail="b.xml: timeout", seen_urls=3)
    )
    store.record_site_run(run2, site_run("c.com", status="blocked", detail="HTTP 403"))
    store.finish_run(run2, NOW - timedelta(minutes=50))
    store.record_urls(
        [found("pf-prende-1", title="PF prende"), found("clima-1")],
        run_id=run2,
        now=NOW - timedelta(hours=1),
        kind=UrlKind.NEW,
    )
    store.record_urls(
        [found("tag-1")],
        run_id=run2,
        now=NOW - timedelta(hours=1),
        kind=UrlKind.FILTERED,
        reasons={"hash-a.com-tag-1": "listing"},
    )
    return store


def test_snapshot_e_serializavel_em_json():
    snapshot = build_snapshot(seeded(), now=NOW)

    assert json.loads(json.dumps(snapshot))["generated_at"] == NOW.isoformat()


def test_funil_acumulado_soma_os_tipos_e_as_que_aguardam_analise():
    snapshot = build_snapshot(seeded(), now=NOW)

    totals = snapshot["totals"]
    assert totals["by_kind"] == {"baseline": 1, "new": 2, "filtered": 1}
    assert totals["urls"] == 4
    funnel = {step["key"]: step["value"] for step in snapshot["funnel"]}
    assert funnel["new"] == 2
    assert funnel["pending"] == 2
    assert "keep" not in funnel and "discard" not in funnel


def test_status_ocioso_quando_a_ultima_rodada_terminou():
    status = build_snapshot(seeded(), now=NOW)["status"]

    assert status["discovery"]["state"] == "idle"
    assert status["last_discovery_finished_at"] == (NOW - timedelta(minutes=50)).isoformat()


def test_rodada_em_andamento_mostra_progresso_e_esperado_pela_anterior():
    store = seeded()
    run3 = store.start_run(NOW - timedelta(minutes=2))
    store.record_site_run(run3, site_run("a.com"))

    discovery = build_snapshot(store, now=NOW)["status"]["discovery"]

    assert discovery["state"] == "running"
    assert discovery["run_id"] == run3
    assert discovery["sites_done"] == 1
    assert discovery["sites_expected"] == 3
    assert discovery["elapsed_seconds"] == 120


def test_rodada_sem_fim_e_muito_antiga_fica_marcada_como_interrompida():
    store = StateStore()
    store.start_run(NOW - timedelta(hours=10))

    assert build_snapshot(store, now=NOW)["status"]["discovery"]["state"] == "stale"


def test_sem_nenhuma_rodada_o_estado_e_never():
    snapshot = build_snapshot(StateStore(), now=NOW)

    assert snapshot["status"]["discovery"]["state"] == "never"
    assert snapshot["runs"] == []
    assert snapshot["recent"] == []


def test_rodadas_trazem_duracao_e_falhas_parciais():
    runs = build_snapshot(seeded(), now=NOW)["runs"]

    assert [r["id"] for r in runs] == [1, 2]
    assert runs[0]["duration_seconds"] == 240
    assert runs[1]["new"] == 2
    assert runs[1]["filtered"] == 1
    assert runs[1]["partial_sites"] == 1
    assert runs[1]["failed"] == 1
    assert (runs[1]["via_sitemap"], runs[1]["via_feed"], runs[1]["via_paid"]) == (0, 0, 0)


def test_fontes_com_problema_vem_primeiro():
    sites = build_snapshot(seeded(), now=NOW)["sites"]

    assert [s["site"] for s in sites] == ["c.com", "b.com", "a.com"]
    c, b, a = sites
    assert (c["status"], c["detail"]) == ("blocked", "HTTP 403")
    assert (b["status"], b["partial_failed"]) == ("ok", 1)
    assert a["new"] == 2
    assert a["urls"]["baseline"] == 1


def test_limite_de_fontes_e_informado():
    snapshot = build_snapshot(seeded(), now=NOW, max_sites=2)

    assert len(snapshot["sites"]) == 2
    assert snapshot["sites_total"] == 3


def test_feed_recente_traz_a_situacao_da_analise_e_a_manchete():
    recent = build_snapshot(with_enrichment(), now=NOW)["recent"]

    by_url = {r["url"]: r for r in recent}
    pf = by_url["https://a.com/pf-prende-1"]
    assert pf["status"] == "in_corpus"
    assert pf["themes"] == ["fiscal"]
    assert pf["headline"] == "PF prende"
    clima = by_url["https://a.com/clima-1"]
    assert clima["status"] == "no_entity"
    assert clima["headline"] == "clima 1"  # slug quando não há título


def test_feed_recente_marca_o_que_ainda_nao_foi_analisado():
    recent = build_snapshot(seeded(), now=NOW)["recent"]

    assert {r["status"] for r in recent} == {"pending"}


def test_serie_por_hora_e_continua_e_preenche_zeros():
    hourly = build_snapshot(with_enrichment(), now=NOW, hours=6)["hourly"]

    assert len(hourly) == 7  # as 6 horas anteriores e a hora corrente
    assert hourly[-1]["hour"] == "2026-10-15T12"
    by_hour = {h["hour"]: h for h in hourly}
    assert by_hour["2026-10-15T11"]["new"] == 2
    assert by_hour["2026-10-15T12"]["meets"] == 1
    assert by_hour["2026-10-15T12"]["not_meets"] == 1
    assert by_hour["2026-10-15T09"] == {
        "hour": "2026-10-15T09",
        "new": 0,
        "meets": 0,
        "not_meets": 0,
    }


def test_fonte_de_cada_site_aparece_na_lista_e_na_rodada():
    store = seeded()
    run = store.start_run(NOW)
    store.record_site_run(run, site_run("a.com", seen_urls=1, source="feed"))
    store.record_site_run(run, site_run("b.com", seen_urls=1, source="sitemap_bd"))

    snapshot = build_snapshot(store, now=NOW)

    by_site = {s["site"]: s for s in snapshot["sites"]}
    assert by_site["a.com"]["source"] == "feed"
    assert by_site["b.com"]["source"] == "sitemap_bd"
    assert by_site["c.com"]["source"] == ""
    last = snapshot["runs"][-1]
    assert (last["via_feed"], last["via_paid"], last["via_sitemap"]) == (1, 1, 0)


def test_motivos_do_l0_e_hosts_em_fallback():
    store = seeded()
    store.add_fallback_hosts(["c.com"], T0)

    snapshot = build_snapshot(store, now=NOW)

    assert snapshot["filter_reasons"] == {"listing": 1}
    assert snapshot["fallback_hosts"] == ["c.com"]


def test_snapshot_traz_o_uso_do_bright_data_por_rodada_e_o_total():
    store = seeded()
    store.record_run_usage(1, bright_data_requests=10, bright_data_hosts=2)
    store.record_run_usage(2, bright_data_requests=5, bright_data_hosts=2)

    snapshot = build_snapshot(store, now=NOW)

    assert [r["bright_data_requests"] for r in snapshot["runs"]] == [10, 5]
    assert snapshot["totals"]["bright_data_requests"] == 15


def test_snapshot_inclui_a_agenda_do_arquivo(tmp_path):
    path = tmp_path / "agenda.json"
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "Rodada 3",
                        "kind": "discovery",
                        "at": (NOW + timedelta(hours=1)).isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    schedule = build_snapshot(seeded(), now=NOW, schedule_path=path)["schedule"]

    assert schedule["source"] == "arquivo"
    assert schedule["jobs"][0]["name"] == "Rodada 3"
    assert schedule["jobs"][0]["seconds_until"] == 3600


def test_snapshot_sem_agenda_nem_cadencia_traz_agenda_vazia():
    schedule = build_snapshot(seeded(), now=NOW)["schedule"]

    assert schedule == {"source": None, "jobs": []}


def with_enrichment() -> StateStore:
    from buscador_midias.state.store import AnalysisRecord, EnrichTotals, EvidenceRecord

    store = seeded()
    run = store.start_enrich_run(
        NOW - timedelta(minutes=30), model="google/x", prompt_version="ctx-v2", evidence_mode="auto"
    )
    entities = ({"name": "João Silva", "type": "PF", "role": "acusado"},)
    store.record_analysis(
        AnalysisRecord(
            "hash-a.com-pf-prende-1",
            True,
            ("fiscal",),
            entities,
            "preso por fraude",
            "resumo",
            0.9,
            "text",
        ),
        enrich_run_id=run,
        model="google/x",
        prompt_version="ctx-v2",
        now=NOW - timedelta(minutes=29),
    )
    store.record_analysis(
        AnalysisRecord("hash-a.com-clima-1", False, (), (), "", "", 0.7, "headline"),
        enrich_run_id=run,
        model="google/x",
        prompt_version="ctx-v2",
        now=NOW - timedelta(minutes=29),
    )
    store.record_evidence(
        EvidenceRecord(
            "hash-a.com-pf-prende-1",
            "captured",
            "direct",
            "midias-ia/evidencias/x.html",
            "ab" * 32,
            1000,
            "text/html",
            200,
            None,
            (),
            None,
        ),
        now=NOW - timedelta(minutes=28),
    )
    store.finish_enrich_run(
        run,
        NOW - timedelta(minutes=27),
        EnrichTotals(
            urls=2,
            fetched_ok=2,
            analyzed=2,
            findings=1,
            evidence_reused=1,
            prompt_tokens=900,
            completion_tokens=100,
            cost_usd=0.002,
        ),
    )
    return store


def test_snapshot_traz_a_secao_de_enriquecimento():
    enrich = build_snapshot(with_enrichment(), now=NOW)["enrich"]

    assert enrich["cost_total_usd"] == 0.002
    assert enrich["runs"][0]["model"] == "google/x"
    assert enrich["runs"][0]["findings"] == 1
    assert enrich["counts"]["analysis"] == {
        "findings": 1,
        "no_finding": 1,
        "headline_only": 1,
        "relevant": 1,
    }
    assert enrich["counts"]["evidence"] == {"captured:direct": 1}
    [finding] = enrich["findings"]
    assert finding["adverse_entities"] == ["João Silva"]
    assert finding["themes"] == ["fiscal"]
    assert finding["evidence_provider"] == "direct"
    assert finding["headline"] == "PF prende"
    assert enrich["reuse_rate"] == 1.0


def test_funil_ganha_analisadas_achados_e_evidencia():
    funnel = {s["key"]: s["value"] for s in build_snapshot(with_enrichment(), now=NOW)["funnel"]}

    assert (funnel["analyzed"], funnel["findings"], funnel["evidence"]) == (2, 1, 1)
    assert funnel["pending"] == 0


def test_estado_do_enriquecimento_ocioso():
    idle = build_snapshot(with_enrichment(), now=NOW)["status"]["enrich"]

    assert idle["state"] == "idle"


def test_estado_do_enriquecimento_nunca_rodou():
    assert build_snapshot(seeded(), now=NOW)["status"]["enrich"]["state"] == "never"


def test_estado_do_enriquecimento_em_execucao():
    store = with_enrichment()
    store.start_enrich_run(
        NOW - timedelta(seconds=40), model="m", prompt_version="v", evidence_mode="auto"
    )

    running = build_snapshot(store, now=NOW)["status"]["enrich"]

    assert running["state"] == "running" and running["elapsed_seconds"] == 40


def test_taxa_de_reaproveitamento_e_nula_sem_evidencias():
    assert build_snapshot(seeded(), now=NOW)["enrich"]["reuse_rate"] is None


def add_url(store: StateStore, url_hash: str) -> None:
    """Registra a URL (a análise exige que ela exista) como ``new`` já vista na rodada 2."""
    store.record_urls(
        [DiscoveredUrl(f"https://a.com/{url_hash}", url_hash, "a.com", "s", None, None, None)],
        run_id=2,
        now=NOW,
        kind=UrlKind.NEW,
    )


def with_corpus() -> StateStore:
    from buscador_midias.state.store import AnalysisRecord, CorpusTotals

    store = with_enrichment()
    add_url(store, "hash-so-manchete")
    store.record_analysis(  # análise só da manchete: não atende ao critério
        AnalysisRecord("hash-so-manchete", True, ("fiscal",), (), "x", "y", 0.8, "headline"),
        enrich_run_id=1,
        model="google/x",
        prompt_version="ctx-v2",
        now=NOW - timedelta(minutes=25),
    )
    run = store.start_corpus_run(NOW - timedelta(minutes=20))
    store.record_corpus_write(
        "hash-a.com-pf-prende-1",
        article_id=10,
        created=True,
        evidence_state="captured",
        corpus_run_id=run,
        now=NOW - timedelta(minutes=19),
    )
    store.finish_corpus_run(
        run,
        NOW - timedelta(minutes=18),
        CorpusTotals(candidates=1, created=1, entities=2, keywords=1),
    )
    return store


def test_snapshot_traz_a_secao_do_corpus():
    corpus = build_snapshot(with_corpus(), now=NOW)["corpus"]

    assert corpus["written"] == 1
    assert corpus["created"] == 1
    assert "withheld" not in corpus
    assert corpus["runs"][0]["created"] == 1
    assert corpus["runs"][0]["entities"] == 2
    assert corpus["runs"][0]["finished_at"] is not None


def test_snapshot_sem_gravacao_tem_corpus_zerado():
    corpus = build_snapshot(seeded(), now=NOW)["corpus"]

    assert (corpus["written"], corpus["created"], corpus["runs"]) == (0, 0, [])


def test_funil_tem_o_passo_gravadas_no_corpus():
    funnel = {s["key"]: s["value"] for s in build_snapshot(with_corpus(), now=NOW)["funnel"]}

    assert funnel["corpus"] == 1


def test_funil_separa_materias_com_entidade_dos_achados_adversos():
    from buscador_midias.state.store import AnalysisRecord

    store = with_enrichment()  # 1 achado adverso (com entidade) e 1 matéria sem entidade
    add_url(store, "hash-neutra")
    store.record_analysis(
        AnalysisRecord(
            "hash-neutra",
            False,
            (),
            ({"name": "Vinícius Júnior", "type": "PF", "role": "outro"},),
            "",
            "resumo",
            0.9,
            "text",
        ),
        enrich_run_id=1,
        model="google/x",
        prompt_version="ctx-v2",
        now=NOW - timedelta(minutes=26),
    )

    steps = build_snapshot(store, now=NOW)["funnel"]
    funnel = {step["key"]: step["value"] for step in steps}

    assert funnel["relevant"] == 2  # o achado adverso e a matéria neutra, ambos com entidade
    assert funnel["findings"] == 1
    order = [step["key"] for step in steps]
    assert order.index("analyzed") < order.index("relevant") < order.index("findings")
