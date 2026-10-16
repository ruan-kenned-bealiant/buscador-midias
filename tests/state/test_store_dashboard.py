import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import (
    AnalysisRecord,
    SiteRunRecord,
    StateStore,
    UrlKind,
)

T0 = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)


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


def test_abertura_somente_leitura_le_mas_nao_grava(tmp_path):
    path = tmp_path / "estado.db"
    writer = StateStore(path)
    run = writer.start_run(T0)
    writer.record_urls([found("x-y")], run_id=run, now=T0, kind=UrlKind.NEW)

    reader = StateStore(path, read_only=True)

    assert reader.count_urls() == 1
    with pytest.raises(sqlite3.OperationalError):
        reader.start_run(T0)
    writer.close()
    reader.close()


def test_contagem_de_urls_por_tipo():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found("a-1"), found("a-2")], run_id=run, now=T0, kind=UrlKind.NEW)
    store.record_urls([found("b-1")], run_id=run, now=T0, kind=UrlKind.BASELINE)

    assert store.count_urls_by_kind() == {"new": 2, "baseline": 1}


def test_urls_novas_por_hora_agrupam_pela_hora_de_descoberta():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found("a-1"), found("a-2")], run_id=run, now=T0, kind=UrlKind.NEW)
    store.record_urls([found("b-1")], run_id=run, now=T0 + timedelta(hours=2), kind=UrlKind.NEW)
    store.record_urls([found("c-1")], run_id=run, now=T0, kind=UrlKind.BASELINE)

    hourly = store.new_urls_per_hour(since=T0 - timedelta(hours=1))

    assert hourly == {"2026-10-15T09": 2, "2026-10-15T11": 1}


def test_urls_novas_por_hora_ignoram_o_que_e_mais_antigo_que_o_limite():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found("a-1")], run_id=run, now=T0 - timedelta(days=3), kind=UrlKind.NEW)

    assert store.new_urls_per_hour(since=T0 - timedelta(days=1)) == {}


def test_ultimas_urls_novas_trazem_a_situacao_da_analise():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls(
        [found("sem-entidade-1"), found("corpus-1", title="No corpus"), found("fila-1")],
        run_id=run,
        now=T0,
        kind=UrlKind.NEW,
    )
    store.record_urls([found("base-1")], run_id=run, now=T0, kind=UrlKind.BASELINE)
    enrich = store.start_enrich_run(T0, model="m", prompt_version="v", evidence_mode="auto")
    for path, entities, themes in (
        ("corpus-1", ({"name": "João Silva", "type": "PF", "role": "citado"},), ("fiscal",)),
        ("sem-entidade-1", (), ()),
    ):
        store.record_analysis(
            AnalysisRecord(f"hash-a.com-{path}", False, themes, entities, "", "s", 0.9, "text"),
            enrich_run_id=enrich,
            model="m",
            prompt_version="v",
            now=T0,
        )

    recent = {r.url.rsplit("/", 1)[1]: r for r in store.recent_new_urls(limit=10)}

    assert set(recent) == {"sem-entidade-1", "corpus-1", "fila-1"}
    assert recent["fila-1"].status == "pending"
    assert recent["sem-entidade-1"].status == "no_entity"
    assert (recent["corpus-1"].status, recent["corpus-1"].themes) == ("in_corpus", ("fiscal",))
    assert recent["corpus-1"].title == "No corpus"
    assert len(store.recent_new_urls(limit=1)) == 1


def test_ultimas_urls_novas_vem_da_mais_recente_para_a_mais_antiga():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found("antiga-1")], run_id=run, now=T0, kind=UrlKind.NEW)
    store.record_urls(
        [found("recente-1")], run_id=run, now=T0 + timedelta(minutes=5), kind=UrlKind.NEW
    )

    recent = store.recent_new_urls(limit=10)

    assert [r.url for r in recent] == ["https://a.com/recente-1", "https://a.com/antiga-1"]


def test_analises_por_hora_separam_as_que_atendem_ao_criterio():
    store = StateStore()
    enrich = store.start_enrich_run(T0, model="m", prompt_version="v", evidence_mode="auto")
    person = ({"name": "João Silva", "type": "PF", "role": "citado"},)

    def analyze(name: str, entities: tuple, basis: str, when: datetime) -> None:
        store.record_analysis(
            AnalysisRecord(f"h-{name}", False, (), entities, "", "s", 0.9, basis),
            enrich_run_id=enrich,
            model="m",
            prompt_version="v",
            now=when,
        )

    analyze("a", person, "text", T0)
    analyze("b", (), "text", T0)
    analyze("c", person, "headline", T0 + timedelta(hours=1))  # só manchete: não atende
    analyze("d", person, "text", T0 - timedelta(days=3))  # antes do limite

    per_hour = store.analyzed_per_hour(since=T0 - timedelta(hours=1))

    assert per_hour == {
        "2026-10-15T09": {"meets": 1, "not_meets": 1},
        "2026-10-15T10": {"not_meets": 1},
    }


def test_resumo_por_fonte_junta_contagens_e_ultima_nova():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found("a-1", site="a.com")], run_id=run, now=T0, kind=UrlKind.NEW)
    store.record_urls(
        [found("a-2", site="a.com"), found("b-1", site="b.com")],
        run_id=run,
        now=T0 + timedelta(hours=1),
        kind=UrlKind.BASELINE,
    )

    counts = store.url_counts_by_site()
    last_new = store.last_new_by_site()

    assert counts == {"a.com": {"new": 1, "baseline": 1}, "b.com": {"baseline": 1}}
    assert last_new == {"a.com": T0}


def test_progresso_da_rodada_conta_os_sites_ja_gravados():
    store = StateStore()
    run = store.start_run(T0)
    store.record_site_run(run, SiteRunRecord("a.com", "ok", 0, 0, 0, 0, 0, 0, ""))
    store.record_site_run(run, SiteRunRecord("b.com", "blocked", 0, 0, 0, 0, 0, 0, ""))

    assert store.sites_done(run) == 2
    assert store.sites_done(run + 1) == 0
