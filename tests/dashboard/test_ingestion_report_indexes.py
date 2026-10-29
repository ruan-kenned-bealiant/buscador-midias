# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Índices por rodada e painel de rodadas: as telas contam por run_id sem varrer o histórico."""

from __future__ import annotations

import os
from functools import partial

import pytest
from psycopg import sql
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest, next_job  # noqa: F401
from test_ingestion_fast_batch import prepare

from buscador_midias.catalog.postgres import connect_postgres

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

NAMES = ("ingest_job_run", "ingest_job_run_created", "ingest_occurrence_run")


def valid(ingest, name):
    """None se o índice não existe; senão se ele está válido."""
    with ingest.connection() as db:
        row = db.execute(
            "SELECT i.indisvalid v FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "WHERE c.relname=%s AND c.relnamespace=%s::regnamespace",
            (name, ingest.store.schema),
        ).fetchone()
    return None if row is None else row["v"]


def autocommit(ingest):
    """Conexão fora de transação, como a que o CREATE INDEX CONCURRENTLY exige."""
    db = connect_postgres(ingest.store.env)
    db.autocommit = True
    db.execute(
        sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(ingest.store.schema))
    )
    return db


def plan(ingest, query, args):
    with ingest.connection() as db:
        db.execute("SET LOCAL enable_seqscan=off")
        rows = db.execute("EXPLAIN " + query, args).fetchall()
    return "\n".join(r["QUERY PLAN"] for r in rows)


def test_new_schema_already_has_the_run_indexes(ingest):
    assert [valid(ingest, n) for n in NAMES] == [True] * len(NAMES)


def test_indexes_serve_the_per_run_filters_of_the_screens(ingest):
    run = ingest.create_run({})
    jobs = plan(ingest, "SELECT count(*) FROM ingest_jobs j WHERE j.run_id=%s", (run["id"],))
    occurrences = plan(
        ingest, "SELECT count(*) FROM ingest_occurrences o WHERE o.run_id=%s", (run["id"],)
    )
    page = plan(
        ingest,
        "SELECT j.id FROM ingest_jobs j WHERE j.run_id=%s ORDER BY j.created_at DESC LIMIT 25",
        (run["id"],),
    )
    assert "ingest_job_run" in jobs
    assert "ingest_occurrence_run" in occurrences
    # A página mais recente sai do índice já ordenada, sem ordenar a rodada inteira.
    assert "ingest_job_run_created" in page and "Sort" not in page


def test_ensure_creates_missing_indexes_once_without_a_transaction(ingest):
    with ingest.connection() as db:
        for name in NAMES:
            db.execute(f"DROP INDEX {name}")
    assert [valid(ingest, n) for n in NAMES] == [None] * len(NAMES)

    assert ingest.ensure_report_indexes() == list(NAMES)

    assert [valid(ingest, n) for n in NAMES] == [True] * len(NAMES)
    assert ingest.ensure_report_indexes() == []


def test_ensure_replaces_an_index_left_invalid_by_an_interrupted_build(ingest):
    prepare(ingest, 2)
    with ingest.connection() as db:
        db.execute("DROP INDEX ingest_job_run")
    # Uma construção concorrente que falha deixa o índice no catálogo, marcado como inválido.
    with autocommit(ingest) as db, pytest.raises(Exception, match="duplicate|duplicad"):
        db.execute("CREATE UNIQUE INDEX CONCURRENTLY ingest_job_run ON ingest_jobs(stage)")
    assert valid(ingest, "ingest_job_run") is False

    assert ingest.ensure_report_indexes() == ["ingest_job_run"]

    assert valid(ingest, "ingest_job_run") is True


def count(db, query, run_id):
    return db.execute(query, (run_id,)).fetchone()["n"]


def test_runs_panel_counts_match_an_independent_count_per_run(ingest):
    first, _ = prepare(ingest, 2)
    ingest.maintain()
    second = ingest.create_run({})

    panel = {r["id"]: r for r in ingest.overview()["runs"]}

    assert set(panel) == {first["id"], second["id"]}
    with ingest.connection() as db:
        for run_id, row in panel.items():
            one = partial(count, db, run_id=run_id)
            pending = "j.state IN ('pending','queued','retry','running') AND "
            recovery = "(j.failures>0 OR j.payload @> '{\"recovery\":true}')"
            assert row["links"] == one("SELECT count(*) n FROM ingest_polls WHERE run_id=%s")
            assert row["links_done"] == one(
                "SELECT count(*) n FROM ingest_polls WHERE run_id=%s AND discovery_done"
            )
            assert row["occurrences"] == one(
                "SELECT count(*) n FROM ingest_occurrences WHERE run_id=%s"
            )
            assert row["unique_articles"] == one(
                "SELECT count(DISTINCT hash) n FROM ingest_occurrences WHERE run_id=%s"
            )
            assert row["primary_pending"] == one(
                f"SELECT count(*) n FROM ingest_jobs j WHERE j.run_id=%s AND {pending}"
                f"NOT {recovery}"
            )
            assert row["recovery_pending"] == one(
                f"SELECT count(*) n FROM ingest_jobs j WHERE j.run_id=%s AND {pending}{recovery}"
            )
    assert panel[first["id"]]["occurrences"] == 2 and panel[second["id"]]["occurrences"] == 0


def test_runs_panel_keeps_the_latest_twenty_newest_first(ingest):
    ids = []
    for _ in range(3):
        ids.append(ingest.create_run({})["id"])
        ingest.maintain()
        with ingest.connection() as db:
            db.execute("UPDATE ingest_polls SET discovery_done=true")
            db.execute("UPDATE ingest_jobs SET state='succeeded' WHERE stage='discovery'")
    assert [r["id"] for r in ingest.overview()["runs"]] == ids[::-1]


def run_with_metrics(ingest):
    """Rodada com uma tarefa de dedup falha que registrou custo e tokens."""
    run, jobs = prepare(ingest, 2)
    ingest.failure(
        jobs[0],
        "falha simulada",
        metrics=dict(cost_usd=0.01, prompt_tokens=5),
        retryable=False,
    )
    return run


def test_costs_are_a_separate_query_and_the_screen_payload_leaves_them_out(ingest):
    run = run_with_metrics(ingest)

    lean = ingest.overview(run_id=run["id"], with_metrics=False)
    full = ingest.overview(run_id=run["id"])

    assert "metrics" not in lean and full["metrics"]
    assert ingest.metrics(run_id=run["id"]) == full["metrics"]
    dedup = next(m for m in full["metrics"] if m["stage"] == "dedup")
    assert float(dedup["cost_usd"]) == pytest.approx(0.01)
    assert dedup["prompt_tokens"] == 5


def test_site_filter_and_unfiltered_screens_agree_with_a_plain_count(ingest):
    run = run_with_metrics(ingest)
    with ingest.connection() as db:
        site = db.execute(
            "SELECT (p.snapshot->'source'->>'id')::bigint id FROM ingest_polls p LIMIT 1"
        ).fetchone()["id"]
        oracle = {
            (r["stage"], r["state"]): r["n"]
            for r in db.execute(
                "SELECT stage,state,count(*) n FROM ingest_jobs WHERE run_id=%s GROUP BY 1,2",
                (run["id"],),
            ).fetchall()
        }
        statuses = {
            r["status"]: r["n"]
            for r in db.execute(
                "SELECT a.status,count(*) n FROM ingest_articles a WHERE a.hash IN "
                "(SELECT hash FROM ingest_occurrences WHERE run_id=%s) GROUP BY 1",
                (run["id"],),
            ).fetchall()
        }
    for source_id in (0, site):
        view = ingest.overview(run_id=run["id"], source_id=source_id)
        assert {(q["stage"], q["state"]): q["n"] for q in view["queues"]} == oracle
        assert view["total"] == sum(oracle.values()) == len(view["jobs"])
        assert {f["status"]: f["n"] for f in view["funnel"]} == statuses
        assert [j["created_at"] for j in view["jobs"]] == sorted(
            (j["created_at"] for j in view["jobs"]), reverse=True
        )
    stage_view = ingest.overview(run_id=run["id"], stage="dedup", state="failed")
    assert stage_view["total"] == oracle.get(("dedup", "failed"), 0)


def test_busca_route_omits_costs_and_custos_route_serves_them(ingest):
    from types import SimpleNamespace

    from buscador_midias.ingestion.api import dispatch

    run = run_with_metrics(ingest)
    api = SimpleNamespace(ingestion=ingest)
    handler = SimpleNamespace(command="GET")

    status, screen = dispatch(api, handler, "/busca", {"run_id": "latest"})
    costs_status, costs = dispatch(api, handler, "/busca/custos", {"run_id": run["id"]})

    assert status == costs_status == 200
    assert "metrics" not in screen and screen["run_id"] == run["id"]
    assert "dedup" in {m["stage"] for m in costs["metrics"]}


@pytest.fixture
def statements(ingest, monkeypatch):
    """Registra os SQLs executados pelo ``overview``; zera o cache de totais das rodadas."""
    from contextlib import contextmanager

    ingest._run_totals.clear()
    seen: list[str] = []
    original = ingest.connection

    class Spy:
        def __init__(self, db):
            self.db = db

        def execute(self, query, *args, **kwargs):
            seen.append(query)
            return self.db.execute(query, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.db, name)

    @contextmanager
    def connection():
        with original() as db:
            yield Spy(db)

    monkeypatch.setattr(ingest, "connection", connection)
    return seen


def totals_reads(seen):
    return [q for q in seen if "unique_articles" in q or "count(DISTINCT o.hash)" in q]


def test_finished_runs_keep_their_totals_instead_of_recounting_them_every_time(ingest, statements):
    first, _ = prepare(ingest, 2)
    ingest.maintain()
    with ingest.connection() as db:
        db.execute("UPDATE ingest_runs SET finished_at=now(),state='completed'")
        db.execute("UPDATE ingest_polls SET discovery_done=true")
    statements.clear()

    one = ingest.overview(with_metrics=False)["runs"]
    counted_once = len(totals_reads(statements))
    two = ingest.overview(with_metrics=False)["runs"]

    assert counted_once >= 1
    assert len(totals_reads(statements)) == counted_once  # a segunda vez não recontou
    assert one == two and one[0]["occurrences"] == 2 and one[0]["id"] == first["id"]


def test_an_open_run_is_always_recounted(ingest, statements):
    prepare(ingest, 2)
    ingest.overview(with_metrics=False)
    reads = len(totals_reads(statements))
    ingest.overview(with_metrics=False)
    assert len(totals_reads(statements)) > reads
