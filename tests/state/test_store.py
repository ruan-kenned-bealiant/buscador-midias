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


def test_seen_hashes_aceita_mais_hashes_que_o_tamanho_de_um_lote():
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


def test_duas_conexoes_veem_o_mesmo_estado():
    first, second = StateStore(), StateStore()
    run = first.start_run(T0)
    first.record_urls([found("a-1")], run_id=run, now=T0, kind=UrlKind.NEW)

    assert second.count_urls() == 1
    assert second.seen_hashes(["hash-a.com-a-1"]) == {"hash-a.com-a-1"}


def test_somente_leitura_le_mas_nao_grava():
    import psycopg
    import pytest

    writer = StateStore()
    run = writer.start_run(T0)
    reader = StateStore(read_only=True)

    assert [r.id for r in reader.list_runs()] == [run]
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        reader.start_run(T0)


def test_sem_variaveis_de_conexao_falha_sem_vazar_a_senha():
    import pytest

    from buscador_midias.state.store import StateConfigError

    with pytest.raises(StateConfigError) as error:
        StateStore({"DB_MIDIAS_IA_PASSWORD": "segredo"})

    assert "DB_MIDIAS_IA_HOST" in str(error.value)
    assert "segredo" not in str(error.value)


def test_schema_com_nome_estranho_e_recusado():
    import os

    import pytest

    from buscador_midias.state.store import StateConfigError

    env = {**os.environ, "BUSCADOR_STATE_SCHEMA": "x; DROP TABLE midias_ia_articles"}
    with pytest.raises(StateConfigError):
        StateStore(env)


def test_banco_sem_as_tabelas_diz_qual_arquivo_rodar():
    import os

    import pytest

    from buscador_midias.state.store import StateSchemaError

    env = {**os.environ, "BUSCADOR_STATE_SCHEMA": "public_sem_tabelas_do_buscador"}
    with pytest.raises(StateSchemaError) as error:
        StateStore(env)

    assert "001_create_midias_crawler_tables.sql" in str(error.value)


def test_texto_de_terceiros_com_caractere_nulo_e_gravado_sem_ele():
    store = StateStore()
    run = store.start_run(T0)
    item = DiscoveredUrl(
        "https://a.com/x\x00y", "h-nul", "a.com", "s", None, "Título\x00 sujo", None
    )

    store.record_urls([item], run_id=run, now=T0, kind=UrlKind.NEW)

    [recent] = store.recent_new_urls(1)
    assert recent.title == "Título sujo"
    assert recent.url == "https://a.com/xy"
