from datetime import UTC, datetime

from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import (
    StateStore,
    TriageRecord,
    TriageRunTotals,
    UrlKind,
)

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def found(path: str, *, title: str | None = None) -> DiscoveredUrl:
    return DiscoveredUrl(
        url=f"https://a.com/{path}",
        url_hash=f"hash-{path}",
        site_key="a.com",
        source_sitemap="https://a.com/sitemap.xml",
        lastmod=None,
        title=title,
        publication_date=None,
    )


def seeded() -> StateStore:
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls(
        [found("um-titulo-aqui", title="Titulo X"), found("dois-sem-titulo")],
        run_id=run,
        now=T0,
        kind=UrlKind.NEW,
    )
    store.record_urls([found("antigo-baseline")], run_id=run, now=T0, kind=UrlKind.BASELINE)
    store.record_urls([found("filtrada-l0")], run_id=run, now=T0, kind=UrlKind.FILTERED)
    return store


def test_so_urls_novas_sem_veredito_estao_pendentes():
    pending = seeded().pending_triage()

    assert [(p.url, p.title, p.site_key) for p in pending] == [
        ("https://a.com/um-titulo-aqui", "Titulo X", "a.com"),
        ("https://a.com/dois-sem-titulo", None, "a.com"),
    ]


def test_url_com_veredito_deixa_de_estar_pendente():
    store = seeded()
    triage_run = store.start_triage_run(T0, model="m", prompt_version="v1")

    store.record_triage(
        [TriageRecord("hash-um-titulo-aqui", "keep", ("fiscal",), "fraude")],
        triage_run_id=triage_run,
        now=T0,
    )

    assert [p.url for p in store.pending_triage()] == ["https://a.com/dois-sem-titulo"]


def test_pendentes_respeitam_o_limite():
    assert len(seeded().pending_triage(limit=1)) == 1


def test_contagem_por_decisao():
    store = seeded()
    run = store.start_triage_run(T0, model="m", prompt_version="v1")
    store.record_triage(
        [
            TriageRecord("hash-um-titulo-aqui", "keep", (), ""),
            TriageRecord("hash-dois-sem-titulo", "discard", (), "clima"),
        ],
        triage_run_id=run,
        now=T0,
    )

    assert store.triage_counts() == {"keep": 1, "discard": 1}


def test_primeiro_veredito_vence():
    store = seeded()
    run = store.start_triage_run(T0, model="m", prompt_version="v1")
    store.record_triage(
        [TriageRecord("hash-um-titulo-aqui", "keep", (), "a")], triage_run_id=run, now=T0
    )

    store.record_triage(
        [TriageRecord("hash-um-titulo-aqui", "discard", (), "b")], triage_run_id=run, now=T0
    )

    assert store.triage_counts() == {"keep": 1}


def test_amostra_de_auditoria_traz_url_motivo_e_modelo():
    store = seeded()
    run = store.start_triage_run(T0, model="google/x", prompt_version="v9")
    store.record_triage(
        [TriageRecord("hash-dois-sem-titulo", "discard", ("criminal",), "esporte")],
        triage_run_id=run,
        now=T0,
    )

    [row] = store.sample_triaged("discard", 5)

    assert (row.url, row.site_key, row.themes, row.reason) == (
        "https://a.com/dois-sem-titulo",
        "a.com",
        ("criminal",),
        "esporte",
    )
    assert (row.model, row.prompt_version) == ("google/x", "v9")
    assert store.sample_triaged("keep", 5) == []


def test_execucao_de_triagem_guarda_totais_e_custo():
    store = StateStore()
    run = store.start_triage_run(T0, model="m", prompt_version="v1")
    totals = TriageRunTotals(
        headlines=10,
        kept=6,
        discarded=3,
        pending=1,
        calls=1,
        prompt_tokens=500,
        completion_tokens=90,
        cost_usd=0.0004,
        retries=1,
        errors=0,
    )

    store.finish_triage_run(run, T0, totals)

    [saved] = store.triage_runs()
    assert (saved.id, saved.model, saved.prompt_version, saved.totals) == (run, "m", "v1", totals)
    assert saved.started_at == T0


def test_custo_desconhecido_fica_nulo():
    store = StateStore()
    run = store.start_triage_run(T0, model="m", prompt_version="v1")

    store.finish_triage_run(run, T0, TriageRunTotals(cost_usd=None))

    assert store.triage_runs()[0].totals.cost_usd is None


def test_reabrir_descartes_so_de_outra_versao_do_prompt_e_so_uma_vez():
    store = seeded()
    old = store.start_triage_run(T0, model="m", prompt_version="v1")
    current = store.start_triage_run(T0, model="m", prompt_version="v2")
    store.record_triage(
        [
            TriageRecord("hash-um-titulo-aqui", "discard", (), "esporte"),
            TriageRecord("hash-dois-sem-titulo", "keep", (), "ok"),
        ],
        triage_run_id=old,
        now=T0,
    )
    store.record_triage(
        [TriageRecord("hash-antigo-baseline", "discard", (), "clima")],
        triage_run_id=current,
        now=T0,
    )

    assert store.reopen_discarded_triage("v2") == 1

    assert [p.url_hash for p in store.pending_triage()] == ["hash-um-titulo-aqui"]
    assert store.triage_counts() == {"keep": 1, "discard": 1}  # o keep e o descarte novo ficam
    assert store.reopen_discarded_triage("v2") == 0
