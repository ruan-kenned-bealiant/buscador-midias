from datetime import UTC, datetime

from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import StateStore, UrlKind

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def found(path: str, site: str = "a.com") -> DiscoveredUrl:
    return DiscoveredUrl(f"https://{site}/{path}", f"h-{site}-{path}", site, "s", None, None, None)


def group_by(store: StateStore) -> dict[str, dict[str, int]]:
    rows = store._all("SELECT site_key, kind, COUNT(*) FROM midias_crawler_urls GROUP BY 1, 2")
    out: dict[str, dict[str, int]] = {}
    for site, kind, count in rows:
        out.setdefault(site, {})[kind] = count
    return out


def test_contadores_acompanham_cada_insercao_por_fonte_e_tipo():
    store = StateStore()
    run = store.start_run(T0)

    store.record_urls([found("a-1"), found("a-2")], run_id=run, now=T0, kind=UrlKind.NEW)
    store.record_urls([found("b-1", "b.com")], run_id=run, now=T0, kind=UrlKind.BASELINE)
    store.record_urls([found("a-3")], run_id=run, now=T0, kind=UrlKind.BASELINE)

    assert store.url_counts_by_site() == {
        "a.com": {"new": 2, "baseline": 1},
        "b.com": {"baseline": 1},
    }
    assert store.count_urls_by_kind() == {"new": 2, "baseline": 2}


def test_url_repetida_nao_conta_duas_vezes_nem_em_outro_tipo():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found("a-1")], run_id=run, now=T0, kind=UrlKind.NEW)

    inserted = store.record_urls(
        [found("a-1"), found("a-1")], run_id=run, now=T0, kind=UrlKind.BASELINE
    )

    assert inserted == 0
    assert store.url_counts_by_site() == {"a.com": {"new": 1}}


def test_repeticao_dentro_do_mesmo_lote_conta_uma_vez():
    store = StateStore()
    run = store.start_run(T0)

    inserted = store.record_urls(
        [found("a-1"), found("a-1"), found("a-2")], run_id=run, now=T0, kind=UrlKind.NEW
    )

    assert inserted == 2
    assert store.count_urls_by_kind() == {"new": 2}


def test_contadores_batem_com_a_contagem_direta_da_tabela():
    store = StateStore()
    run = store.start_run(T0)
    for site in ("a.com", "b.com", "c.com"):
        store.record_urls(
            [found(f"{n}", site) for n in range(25)], run_id=run, now=T0, kind=UrlKind.BASELINE
        )
        store.record_urls(
            [found(f"n{n}", site) for n in range(7)], run_id=run, now=T0, kind=UrlKind.NEW
        )
        store.record_urls(
            [found(f"x{n}", site) for n in range(3)], run_id=run, now=T0, kind=UrlKind.FILTERED
        )

    assert store.url_counts_by_site() == group_by(store)


def test_a_leitura_vem_dos_contadores_e_nao_de_uma_varredura_da_tabela():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found("a-1"), found("a-2")], run_id=run, now=T0, kind=UrlKind.NEW)
    store._run("DELETE FROM midias_crawler_urls")  # se lesse da tabela, voltaria vazio

    assert store.count_urls_by_kind() == {"new": 2}
    assert store.url_counts_by_site() == {"a.com": {"new": 2}}


def test_contagens_de_pendentes_batem_com_as_listas():
    from buscador_midias.state.store import AnalysisRecord, EvidenceRecord

    store = StateStore()
    run = store.start_run(T0)
    paths = [f"p-{n}" for n in range(5)]
    store.record_urls([found(p) for p in paths], run_id=run, now=T0, kind=UrlKind.NEW)
    enrich = store.start_enrich_run(T0, model="m", prompt_version="v", evidence_mode="auto")
    for p in paths[:2]:
        store.record_analysis(
            AnalysisRecord(
                f"h-a.com-{p}",
                True,
                (),
                ({"name": "X Y", "type": "PF", "role": "acusado"},),
                "f",
                "s",
                0.9,
                "text",
            ),
            enrich_run_id=enrich,
            model="m",
            prompt_version="v",
            now=T0,
        )
    store.record_evidence(
        EvidenceRecord(
            "h-a.com-p-0", "captured", "direct", "p", "ab" * 32, 1, "text/html", 200, None, (), None
        ),
        now=T0,
    )

    assert store.count_pending_enrichment() == len(store.pending_enrichment()) == 3
    assert store.count_pending_evidence() == len(store.pending_evidence()) == 1


def test_leitura_somente_leitura_usa_os_contadores():
    writer = StateStore()
    run = writer.start_run(T0)
    writer.record_urls([found("a-1")], run_id=run, now=T0, kind=UrlKind.NEW)

    reader = StateStore(read_only=True)

    assert reader.count_urls_by_kind() == {"new": 1}


def test_consultas_de_novas_usam_o_indice_por_tipo_e_data():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found(f"a-{n}") for n in range(50)], run_id=run, now=T0, kind=UrlKind.NEW)
    store._run("ANALYZE midias_crawler_urls")
    queries = [
        "SELECT url FROM midias_crawler_urls WHERE kind = 'new'"
        " ORDER BY first_seen_at DESC LIMIT 10",
        "SELECT COUNT(*) FROM midias_crawler_urls WHERE kind = 'new'"
        " AND first_seen_at >= '2026-01-01'",
    ]
    store._run("SET enable_seqscan = off")  # com poucas linhas o planejador preferiria varrer

    for query in queries:
        plan = " ".join(row[0] for row in store._all("EXPLAIN " + query))
        assert "midias_crawler_urls_kind_seen" in plan, plan
