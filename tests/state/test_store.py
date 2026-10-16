import threading
from datetime import UTC, datetime, timedelta

from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import SiteRunRecord, StateStore, UrlKind

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def found(path: str, *, site: str = "a.com", published: datetime | None = None) -> DiscoveredUrl:
    url = f"https://{site}/{path}"
    return DiscoveredUrl(
        url=url,
        url_hash=f"hash-{site}-{path}",
        site_key=site,
        source_sitemap=f"https://{site}/sitemap.xml",
        lastmod=None,
        title=None,
        publication_date=published,
    )


def test_runs_tem_ids_crescentes_e_guardam_inicio_e_fim():
    store = StateStore()

    first = store.start_run(T0)
    second = store.start_run(T0 + timedelta(hours=8))
    store.finish_run(first, T0 + timedelta(minutes=5))

    assert (first, second) == (1, 2)
    runs = store.list_runs()
    assert [r.id for r in runs] == [1, 2]
    assert runs[0].started_at == T0
    assert runs[0].finished_at == T0 + timedelta(minutes=5)
    assert runs[1].finished_at is None


def test_record_urls_insere_novas_e_ignora_as_ja_vistas():
    store = StateStore()
    run = store.start_run(T0)

    inserted_first = store.record_urls(
        [found("a"), found("b")], run_id=run, now=T0, kind=UrlKind.NEW
    )
    inserted_again = store.record_urls(
        [found("b"), found("c")], run_id=run, now=T0, kind=UrlKind.NEW
    )

    assert inserted_first == 2
    assert inserted_again == 1
    assert store.seen_hashes(["hash-a.com-a", "hash-a.com-z"]) == {"hash-a.com-a"}


def test_a_primeira_classificacao_da_url_vence():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found("a")], run_id=run, now=T0, kind=UrlKind.BASELINE)

    store.record_urls([found("a")], run_id=run, now=T0, kind=UrlKind.NEW)

    assert store.count_urls(kind=UrlKind.BASELINE) == 1
    assert store.count_urls(kind=UrlKind.NEW) == 0


def test_seen_hashes_aceita_mais_hashes_que_o_limite_de_variaveis_do_sqlite():
    store = StateStore()
    run = store.start_run(T0)
    urls = [found(f"p{i}") for i in range(2500)]
    store.record_urls(urls, run_id=run, now=T0, kind=UrlKind.BASELINE)

    seen = store.seen_hashes([u.url_hash for u in urls] + ["fora"])

    assert len(seen) == 2500


def test_baseline_do_site_e_idempotente():
    store = StateStore()
    run = store.start_run(T0)

    assert store.site_has_baseline("a.com") is False
    store.mark_baseline("a.com", run)
    store.mark_baseline("a.com", run + 1)

    assert store.site_has_baseline("a.com") is True
    assert store.site_has_baseline("b.com") is False


def test_estado_do_sitemap_guarda_o_ultimo_lastmod_visto():
    store = StateStore()
    stamp = datetime(2026, 10, 14, tzinfo=UTC)

    assert store.sitemap_lastmod("https://a.com/s1.xml") is None
    store.record_sitemap("https://a.com/s1.xml", "a.com", stamp, T0)
    store.record_sitemap("https://a.com/s1.xml", "a.com", stamp + timedelta(days=1), T0)

    assert store.sitemap_lastmod("https://a.com/s1.xml") == stamp + timedelta(days=1)


def test_sitemap_sem_lastmod_declarado_nao_tem_lastmod_para_comparar():
    store = StateStore()

    store.record_sitemap("https://a.com/s.xml", "a.com", None, T0)

    assert store.sitemap_lastmod("https://a.com/s.xml") is None


def test_resultados_por_site_ficam_ligados_a_rodada():
    store = StateStore()
    run = store.start_run(T0)
    record = SiteRunRecord(
        site_key="a.com",
        status="ok",
        new_urls=3,
        baseline_urls=0,
        backlog_urls=1,
        seen_urls=10,
        sitemaps_fetched=2,
        sitemaps_skipped=1,
        detail="",
    )

    store.record_site_run(run, record)

    assert store.site_runs(run) == [record]
    assert store.site_runs(run + 1) == []


def test_all_site_runs_junta_todas_as_rodadas_em_ordem():
    store = StateStore()
    first, second = store.start_run(T0), store.start_run(T0)
    for run, site in ((second, "b.com"), (first, "z.com"), (first, "a.com"), (second, "a.com")):
        store.record_site_run(run, SiteRunRecord(site, "ok", 0, 0, 0, 0, 0, 0, ""))

    pairs = [(run, rec.site_key) for run, rec in store.all_site_runs()]

    assert pairs == [(first, "a.com"), (first, "z.com"), (second, "a.com"), (second, "b.com")]


def test_urls_ja_conhecidas_pelo_corpus_tem_tipo_proprio():
    store = StateStore()
    run = store.start_run(T0)

    store.record_urls([found("a"), found("b")], run_id=run, now=T0, kind=UrlKind.KNOWN)

    assert store.count_urls(kind=UrlKind.KNOWN) == 2
    assert store.count_urls(kind=UrlKind.NEW) == 0


def test_resultado_do_site_guarda_quantas_urls_o_corpus_ja_conhecia():
    store = StateStore()
    run = store.start_run(T0)
    record = SiteRunRecord(
        site_key="a.com",
        status="ok",
        new_urls=1,
        baseline_urls=0,
        backlog_urls=0,
        seen_urls=5,
        sitemaps_fetched=1,
        sitemaps_skipped=0,
        detail="",
        known_urls=4,
    )

    store.record_site_run(run, record)

    assert store.site_runs(run)[0].known_urls == 4


def test_hosts_de_fallback_persistem_e_nao_duplicam():
    store = StateStore()

    store.add_fallback_hosts(["a.com", "b.com"], T0)
    store.add_fallback_hosts(["b.com", "c.com"], T0)

    assert store.fallback_hosts() == frozenset({"a.com", "b.com", "c.com"})


def test_latencia_de_publicacao_so_conta_urls_novas_com_data():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls(
        [
            found("a", published=T0 - timedelta(hours=2)),
            found("b", published=T0 - timedelta(hours=5)),
        ],
        run_id=run,
        now=T0,
        kind=UrlKind.NEW,
    )
    store.record_urls([found("c")], run_id=run, now=T0, kind=UrlKind.NEW)
    store.record_urls(
        [found("d", published=T0 - timedelta(days=1))], run_id=run, now=T0, kind=UrlKind.BASELINE
    )

    assert sorted(store.new_url_latencies_hours()) == [2.0, 5.0]


def test_persiste_em_arquivo_entre_aberturas(tmp_path):
    path = tmp_path / "estado.db"
    first = StateStore(path)
    run = first.start_run(T0)
    first.record_urls([found("a")], run_id=run, now=T0, kind=UrlKind.BASELINE)
    first.close()

    second = StateStore(path)

    assert second.seen_hashes(["hash-a.com-a"]) == {"hash-a.com-a"}
    assert second.start_run(T0) == 2


def test_escritas_concorrentes_de_varias_threads_nao_se_perdem():
    store = StateStore()
    run = store.start_run(T0)

    def work(worker: int) -> None:
        for batch in range(20):
            urls = [found(f"w{worker}-b{batch}-{i}", site=f"s{worker}.com") for i in range(10)]
            store.record_urls(urls, run_id=run, now=T0, kind=UrlKind.NEW)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.count_urls() == 8 * 20 * 10


def test_resultado_do_site_guarda_quantos_sitemaps_falharam():
    store = StateStore()
    run = store.start_run(T0)
    record = SiteRunRecord("a.com", "ok", 0, 0, 0, 5, 3, 0, "b.xml: timeout", sitemaps_failed=2)

    store.record_site_run(run, record)

    assert store.site_runs(run) == [record]
    assert store.all_site_runs() == [(run, record)]


def test_banco_criado_antes_da_coluna_de_falhas_e_migrado_sem_perder_dados(tmp_path):
    import sqlite3

    path = tmp_path / "antigo.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE site_runs (
            run_id INTEGER NOT NULL, site_key TEXT NOT NULL, status TEXT NOT NULL,
            new_urls INTEGER NOT NULL, baseline_urls INTEGER NOT NULL,
            backlog_urls INTEGER NOT NULL, seen_urls INTEGER NOT NULL,
            sitemaps_fetched INTEGER NOT NULL, sitemaps_skipped INTEGER NOT NULL,
            detail TEXT NOT NULL, known_urls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, site_key)
        );
        INSERT INTO site_runs VALUES (1, 'a.com', 'ok', 4, 0, 0, 4, 1, 0, '', 0);
        """
    )
    legacy.commit()
    legacy.close()

    store = StateStore(path)

    [record] = store.site_runs(1)
    assert record.new_urls == 4
    assert record.sitemaps_failed == 0


def test_url_filtrada_guarda_o_motivo_e_so_ela():
    store = StateStore()
    run = store.start_run(T0)

    store.record_urls(
        [found("a"), found("b")],
        run_id=run,
        now=T0,
        kind=UrlKind.FILTERED,
        reasons={"hash-a.com-a": "listing", "hash-a.com-b": "section"},
    )
    store.record_urls([found("c")], run_id=run, now=T0, kind=UrlKind.NEW)

    assert store.filtered_by_reason() == {"listing": 1, "section": 1}
    assert store.count_urls(kind=UrlKind.FILTERED) == 2


def test_resultado_do_site_guarda_quantas_urls_o_l0_descartou():
    store = StateStore()
    run = store.start_run(T0)
    record = SiteRunRecord("a.com", "ok", 1, 0, 0, 5, 1, 0, "", filtered_urls=4)

    store.record_site_run(run, record)

    assert store.site_runs(run) == [record]


def test_banco_antigo_ganha_as_colunas_do_l0(tmp_path):
    import sqlite3

    path = tmp_path / "antigo.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE urls_seen (
            url_hash TEXT PRIMARY KEY, url TEXT NOT NULL, site_key TEXT NOT NULL,
            source_sitemap TEXT NOT NULL, kind TEXT NOT NULL, first_seen_run INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL, lastmod TEXT, title TEXT, publication_date TEXT
        );
        INSERT INTO urls_seen VALUES ('h', 'https://a.com/x', 'a.com', 's', 'new', 1, 't',
                                      NULL, NULL, NULL);
        CREATE TABLE site_runs (
            run_id INTEGER NOT NULL, site_key TEXT NOT NULL, status TEXT NOT NULL,
            new_urls INTEGER NOT NULL, baseline_urls INTEGER NOT NULL,
            backlog_urls INTEGER NOT NULL, seen_urls INTEGER NOT NULL,
            sitemaps_fetched INTEGER NOT NULL, sitemaps_skipped INTEGER NOT NULL,
            detail TEXT NOT NULL, known_urls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, site_key)
        );
        INSERT INTO site_runs VALUES (1, 'a.com', 'ok', 4, 0, 0, 4, 1, 0, '', 0);
        """
    )
    legacy.commit()
    legacy.close()

    store = StateStore(path)

    assert store.count_urls() == 1
    assert store.filtered_by_reason() == {}
    assert store.site_runs(1)[0].filtered_urls == 0


def test_resultado_do_site_guarda_de_que_fonte_vieram_as_urls():
    store = StateStore()
    run = store.start_run(T0)
    via_feed = SiteRunRecord("a.com", "ok", 2, 0, 0, 2, 1, 0, "", source="feed")
    failed = SiteRunRecord("b.com", "blocked", 0, 0, 0, 0, 1, 0, "sitemap: 403")

    store.record_site_run(run, via_feed)
    store.record_site_run(run, failed)

    assert store.site_runs(run) == [via_feed, failed]
    assert failed.source == ""
    assert [r.source for _, r in store.all_site_runs()] == ["feed", ""]


def test_banco_antigo_ganha_a_coluna_da_fonte_da_descoberta(tmp_path):
    import sqlite3

    path = tmp_path / "antigo.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE site_runs (
            run_id INTEGER NOT NULL, site_key TEXT NOT NULL, status TEXT NOT NULL,
            new_urls INTEGER NOT NULL, baseline_urls INTEGER NOT NULL,
            backlog_urls INTEGER NOT NULL, seen_urls INTEGER NOT NULL,
            sitemaps_fetched INTEGER NOT NULL, sitemaps_skipped INTEGER NOT NULL,
            detail TEXT NOT NULL, known_urls INTEGER NOT NULL DEFAULT 0,
            sitemaps_failed INTEGER NOT NULL DEFAULT 0, filtered_urls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, site_key)
        );
        INSERT INTO site_runs VALUES (1, 'a.com', 'ok', 4, 0, 0, 4, 1, 0, '', 0, 0, 0);
        """
    )
    legacy.commit()
    legacy.close()

    store = StateStore(path)

    assert store.site_runs(1)[0].source == ""


def test_rodada_guarda_o_uso_do_bright_data():
    store = StateStore()
    run = store.start_run(T0)
    store.finish_run(run, T0 + timedelta(minutes=2))

    store.record_run_usage(run, bright_data_requests=7, bright_data_hosts=2)

    [record] = store.list_runs()
    assert (record.bright_data_requests, record.bright_data_hosts) == (7, 2)


def test_rodada_sem_uso_registrado_tem_zero():
    store = StateStore()
    store.start_run(T0)

    [record] = store.list_runs()

    assert (record.bright_data_requests, record.bright_data_hosts) == (0, 0)


def test_banco_antigo_ganha_as_colunas_de_uso_do_bright_data(tmp_path):
    import sqlite3

    path = tmp_path / "antigo.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
                           finished_at TEXT);
        INSERT INTO runs (started_at) VALUES ('2026-10-15T09:00:00+00:00');
        """
    )
    legacy.commit()
    legacy.close()

    store = StateStore(path)

    assert store.list_runs()[0].bright_data_requests == 0
    store.record_run_usage(1, bright_data_requests=3, bright_data_hosts=1)
    assert store.list_runs()[0].bright_data_requests == 3


def test_backup_gera_copia_consistente_enquanto_o_original_segue_em_uso(tmp_path):
    source = StateStore(tmp_path / "estado.db")
    run = source.start_run(T0)
    source.record_urls([found("a-1"), found("a-2")], run_id=run, now=T0, kind=UrlKind.NEW)
    source.add_fallback_hosts(["x.com"], T0)

    destination = tmp_path / "copia" / "estado-backup.db"
    source.backup(destination)
    source.record_urls([found("a-3")], run_id=run, now=T0, kind=UrlKind.NEW)  # depois do backup

    copy = StateStore(destination, read_only=True)
    assert copy.count_urls() == 2
    assert copy.fallback_hosts() == frozenset({"x.com"})
    assert source.count_urls() == 3
    assert copy.integrity_ok()
    copy.close()


def test_backup_nao_sobrescreve_arquivo_existente(tmp_path):
    import pytest

    source = StateStore(tmp_path / "estado.db")
    destination = tmp_path / "ja-existe.db"
    destination.write_bytes(b"nao apague")

    with pytest.raises(FileExistsError):
        source.backup(destination)

    assert destination.read_bytes() == b"nao apague"


def test_backup_funciona_a_partir_de_uma_abertura_somente_leitura(tmp_path):
    writer = StateStore(tmp_path / "estado.db")
    run = writer.start_run(T0)
    writer.record_urls([found("a-1")], run_id=run, now=T0, kind=UrlKind.NEW)
    reader = StateStore(tmp_path / "estado.db", read_only=True)

    reader.backup(tmp_path / "copia.db")

    assert StateStore(tmp_path / "copia.db", read_only=True).count_urls() == 1
