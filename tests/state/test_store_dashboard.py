import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import (
    SiteRunRecord,
    StateStore,
    TriageRecord,
    TriageRunTotals,
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


def test_vereditos_por_hora_e_decisao():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls(
        [found("a-1"), found("a-2"), found("a-3")], run_id=run, now=T0, kind=UrlKind.NEW
    )
    triage = store.start_triage_run(T0, model="m", prompt_version="v")
    store.record_triage(
        [
            TriageRecord("hash-a.com-a-1", "keep", (), ""),
            TriageRecord("hash-a.com-a-2", "discard", (), ""),
        ],
        triage_run_id=triage,
        now=T0,
    )
    store.record_triage(
        [TriageRecord("hash-a.com-a-3", "keep", (), "")],
        triage_run_id=triage,
        now=T0 + timedelta(hours=1),
    )

    per_hour = store.triage_per_hour(since=T0 - timedelta(hours=1))

    assert per_hour == {
        "2026-10-15T09": {"keep": 1, "discard": 1},
        "2026-10-15T10": {"keep": 1},
    }


def test_ultimas_urls_novas_trazem_o_veredito_quando_existe():
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls([found("antiga-1", title="Antiga")], run_id=run, now=T0, kind=UrlKind.NEW)
    store.record_urls(
        [found("recente-1")], run_id=run, now=T0 + timedelta(minutes=5), kind=UrlKind.NEW
    )
    store.record_urls([found("base-1")], run_id=run, now=T0, kind=UrlKind.BASELINE)
    triage = store.start_triage_run(T0, model="m", prompt_version="v")
    store.record_triage(
        [TriageRecord("hash-a.com-antiga-1", "discard", ("criminal",), "clima")],
        triage_run_id=triage,
        now=T0,
    )

    recent = store.recent_new_urls(limit=10)

    assert [r.url for r in recent] == ["https://a.com/recente-1", "https://a.com/antiga-1"]
    assert recent[0].decision is None
    assert (recent[1].decision, recent[1].themes, recent[1].reason) == (
        "discard",
        ("criminal",),
        "clima",
    )
    assert recent[1].title == "Antiga"
    assert len(store.recent_new_urls(limit=1)) == 1


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


def test_execucao_da_triagem_informa_se_terminou():
    store = StateStore()
    open_run = store.start_triage_run(T0, model="m", prompt_version="v")
    done_run = store.start_triage_run(T0, model="m", prompt_version="v")
    store.finish_triage_run(done_run, T0 + timedelta(minutes=1), TriageRunTotals(headlines=1))

    runs = {r.id: r for r in store.triage_runs()}

    assert runs[open_run].finished_at is None
    assert runs[done_run].finished_at == T0 + timedelta(minutes=1)


def test_progresso_da_rodada_conta_os_sites_ja_gravados():
    store = StateStore()
    run = store.start_run(T0)
    store.record_site_run(run, SiteRunRecord("a.com", "ok", 0, 0, 0, 0, 0, 0, ""))
    store.record_site_run(run, SiteRunRecord("b.com", "blocked", 0, 0, 0, 0, 0, 0, ""))

    assert store.sites_done(run) == 2
    assert store.sites_done(run + 1) == 0
