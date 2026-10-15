import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import StateStore, UrlKind

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def found(path: str, site: str = "a.com") -> DiscoveredUrl:
    return DiscoveredUrl(f"https://{site}/{path}", f"h-{site}-{path}", site, "s", None, None, None)


def group_by(store: StateStore) -> dict[str, dict[str, int]]:
    rows = store._conn.execute(
        "SELECT site_key, kind, COUNT(*) FROM urls_seen GROUP BY 1, 2"
    ).fetchall()
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


def test_banco_antigo_sem_contadores_e_reconstruido_na_abertura(tmp_path: Path):
    path = tmp_path / "antigo.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE urls_seen (
            url_hash TEXT PRIMARY KEY, url TEXT NOT NULL, site_key TEXT NOT NULL,
            source_sitemap TEXT NOT NULL, kind TEXT NOT NULL, first_seen_run INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL, lastmod TEXT, title TEXT, publication_date TEXT
        );
        INSERT INTO urls_seen VALUES ('h1','u1','a.com','s','new',1,'t',NULL,NULL,NULL);
        INSERT INTO urls_seen VALUES ('h2','u2','a.com','s','baseline',1,'t',NULL,NULL,NULL);
        INSERT INTO urls_seen VALUES ('h3','u3','b.com','s','baseline',1,'t',NULL,NULL,NULL);
        """
    )
    legacy.commit()
    legacy.close()

    store = StateStore(path)

    assert store.url_counts_by_site() == {
        "a.com": {"new": 1, "baseline": 1},
        "b.com": {"baseline": 1},
    }
    run = store.start_run(T0)
    store.record_urls([found("a-9")], run_id=run, now=T0, kind=UrlKind.NEW)
    assert store.url_counts_by_site()["a.com"]["new"] == 2


def test_abrir_de_novo_nao_reconta_nem_duplica(tmp_path: Path):
    path = tmp_path / "estado.db"
    first = StateStore(path)
    run = first.start_run(T0)
    first.record_urls([found("a-1"), found("a-2")], run_id=run, now=T0, kind=UrlKind.NEW)
    first.close()

    second = StateStore(path)

    assert second.url_counts_by_site() == {"a.com": {"new": 2}}


def test_leitura_somente_leitura_usa_os_contadores(tmp_path: Path):
    path = tmp_path / "estado.db"
    writer = StateStore(path)
    run = writer.start_run(T0)
    writer.record_urls([found("a-1")], run_id=run, now=T0, kind=UrlKind.NEW)

    reader = StateStore(path, read_only=True)

    assert reader.count_urls_by_kind() == {"new": 1}


def test_a_leitura_vem_dos_contadores_e_nao_de_uma_varredura_da_tabela():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found("a-1"), found("a-2")], run_id=run, now=T0, kind=UrlKind.NEW)
    store._conn.execute("DELETE FROM urls_seen")  # se lesse da tabela, voltaria vazio

    assert store.count_urls_by_kind() == {"new": 2}
    assert store.url_counts_by_site() == {"a.com": {"new": 2}}


def test_ha_indice_por_tipo_e_data_para_as_consultas_de_novas():
    store = StateStore()

    indexes = {row[1] for row in store._conn.execute("PRAGMA index_list(urls_seen)")}

    assert "idx_urls_kind_seen" in indexes


def test_consultas_de_novas_usam_o_indice_e_nao_varrem_a_tabela():
    store = StateStore()
    queries = [
        "SELECT url FROM urls_seen WHERE kind = 'new' ORDER BY first_seen_at DESC LIMIT 10",
        "SELECT COUNT(*) FROM urls_seen WHERE kind = 'new' AND first_seen_at >= 'x'",
    ]

    for query in queries:
        plan = " ".join(row[3] for row in store._conn.execute("EXPLAIN QUERY PLAN " + query))
        assert "idx_urls_kind_seen" in plan, plan


def test_contagens_de_pendentes_batem_com_as_listas():
    from buscador_midias.state.store import AnalysisRecord, EvidenceRecord, TriageRecord

    store = StateStore()
    run = store.start_run(T0)
    paths = [f"p-{n}" for n in range(5)]
    store.record_urls([found(p) for p in paths], run_id=run, now=T0, kind=UrlKind.NEW)
    triage = store.start_triage_run(T0, model="m", prompt_version="v")
    store.record_triage(
        [TriageRecord(f"h-a.com-{p}", "keep", (), "") for p in paths[:4]],
        triage_run_id=triage,
        now=T0,
    )
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

    assert store.count_pending_triage() == len(store.pending_triage()) == 1
    assert store.count_pending_enrichment() == len(store.pending_enrichment()) == 2
    assert store.count_pending_evidence() == len(store.pending_evidence()) == 1
