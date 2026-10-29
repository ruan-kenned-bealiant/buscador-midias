"""Pool de conexões curtas: reutiliza conexões, nunca passa do limite e deixa o lock à parte."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from buscador_midias.db.store import WorkStore

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.fixture
def store(schema_name: str):
    work = WorkStore(schema_name)
    work.install()
    yield work
    work.close_pool()


def backend_pid(store: WorkStore) -> int:
    with store.connection() as db:
        return int(db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])


def test_without_a_pool_every_operation_opens_its_own_connection(store):
    assert backend_pid(store) != backend_pid(store)


def test_with_a_pool_connections_are_reused(store):
    store.start_pool(min_size=1, max_size=1)
    assert backend_pid(store) == backend_pid(store)


def test_the_pool_uses_the_right_schema_and_commits_on_return(store, schema_name):
    store.start_pool(min_size=1, max_size=2)
    with store.connection() as db:
        assert db.execute("SELECT current_schema() AS s").fetchone()["s"] == schema_name
        db.execute("INSERT INTO bm_host_state(host) VALUES('pooled.example')")
    with store.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM bm_host_state").fetchone()["n"] == 1


def test_an_error_rolls_back_and_the_connection_stays_usable(store):
    store.start_pool(min_size=1, max_size=1)
    with pytest.raises(RuntimeError), store.connection() as db:
        db.execute("INSERT INTO bm_host_state(host) VALUES('x.example')")
        raise RuntimeError("falhou")
    with store.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM bm_host_state").fetchone()["n"] == 0


def test_many_threads_share_a_small_pool(store):
    store.start_pool(min_size=1, max_size=3)

    def work(_: int) -> int:
        return backend_pid(store)

    with ThreadPoolExecutor(max_workers=12) as threads:
        pids = set(threads.map(work, range(60)))
    assert 1 <= len(pids) <= 3


def test_the_lease_connection_never_uses_the_pool(store):
    store.start_pool(min_size=1, max_size=1)
    with store.connection() as pooled, store.lease_connection() as lease:
        mine = pooled.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        other = lease.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    assert mine != other


def test_closing_the_pool_returns_to_direct_connections(store):
    store.start_pool(min_size=1, max_size=1)
    store.close_pool()
    assert backend_pid(store) != backend_pid(store)
    store.close_pool()  # repetir é seguro
