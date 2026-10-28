# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Corte pelo topo na descoberta: sombra só mede, ativo dispensa o documento inteiro."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_catalog_testing import FakeReader
from test_ingestion import ingest, next_job  # noqa: F401

from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion.engine import Engine
from buscador_midias.net.fetcher import FetchResult

needs_postgres = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def feed(ages_in_hours, since):
    """Feed com um item por idade (horas antes do início da janela; negativa = dentro dela)."""
    items = "".join(
        f"<item><link>https://jose.example/n{i}</link><pubDate>"
        f"{(since - timedelta(hours=h)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for i, h in enumerate(ages_in_hours)
    )
    return f"<rss><channel>{items}</channel></rss>".encode()


class StoppableReader(FakeReader):
    """Leitor que aceita parada antecipada, como o direto e o curl."""

    supports_early_stop = True

    def __init__(self, data, progress):
        super().__init__(data, progress)
        self.asked = False

    def get(self, url, *, early_stop=None):
        result = super().get(url)
        if early_stop is None:
            return result
        self.asked = True
        if early_stop(result.content):
            return FetchResult(url, 200, result.content[:200], None, url, truncated=True)
        return result


def discover(ingest, mode, ages, *, order="newest_first", reader=FakeReader):
    ingest.configure(
        dict(
            version=ingest.settings()["version"],
            config=dict(
                window_mode="today", head_filter=mode, lastmod_filter="off", url_filter="off"
            ),
        )
    )
    ingest.create_run({})
    job = next_job(ingest, "discovery")
    job["snapshot"].update(
        model="rss", kind="feed", parameters=get_mechanism("rss").defaults(), read_order=order
    )
    body = feed(ages, job["run"]["since"])
    made = []

    def factory(roots, progress):
        made.append(reader({job["payload"]["url"]: body}, progress))
        return made[0]

    result = Engine(ingest, reader_factory=factory).discovery(job)
    return result, made[0]


OLD = [30 + i for i in range(40)]
LIVE = [-2, -1, *range(1, 38)]
PINNED_OLD_ON_TOP = [60, -2, -1, 1, 2, 3, 4, 5, 6, 7, 8]


@needs_postgres
def test_shadow_reads_everything_and_records_what_it_would_have_skipped(ingest):
    result, _ = discover(ingest, "shadow", OLD)
    assert result["metrics"]["head_shadow_would_skip"] == 1
    assert result["metrics"].get("head_shadow_missed", 0) == 0
    assert result["shadow_head_exclusion"]["rule"] == "head_before_window"


@needs_postgres
def test_a_pinned_old_item_on_top_is_a_contradiction_not_a_cut(ingest):
    # Rótulo newest_first, mas o documento tem notícia da janela abaixo de um item fixado.
    result, _ = discover(ingest, "shadow", PINNED_OLD_ON_TOP)
    assert result["metrics"]["read_order_contradicted"] == 1
    assert not result["metrics"].get("head_shadow_would_skip")
    assert result["candidates"]


@needs_postgres
def test_shadow_does_not_flag_a_live_document(ingest):
    result, _ = discover(ingest, "shadow", LIVE)
    assert not result["metrics"].get("head_shadow_would_skip")
    assert "shadow_head_exclusion" not in result


@needs_postgres
def test_enforce_without_early_stop_discards_the_whole_document(ingest):
    result, _ = discover(ingest, "enforce", OLD)
    assert result["candidates"] == [] and result["children"] == []
    assert result["metrics"]["head_cut"] == 1
    assert result["outcome"] == "sem_novidades"


@needs_postgres
def test_enforce_stops_the_download_when_the_reader_can_do_it(ingest):
    result, reader = discover(ingest, "enforce", OLD, reader=StoppableReader)
    assert reader.asked
    assert result["candidates"] == [] and result["outcome"] == "sem_novidades"
    assert result["metrics"]["head_stopped_download"] == 1


@needs_postgres
def test_enforce_never_cuts_a_live_document(ingest):
    result, reader = discover(ingest, "enforce", LIVE, reader=StoppableReader)
    assert reader.asked
    assert result["candidates"]
    assert not result["metrics"].get("head_cut")


@needs_postgres
@pytest.mark.parametrize("order", ["unknown", "unordered", "oldest_first"])
def test_without_a_proven_order_nothing_is_cut_or_stopped(ingest, order):
    result, reader = discover(
        ingest, "enforce", PINNED_OLD_ON_TOP, order=order, reader=StoppableReader
    )
    assert not reader.asked
    assert [c["url"].rsplit("/", 1)[1] for c in result["candidates"]]
    assert not result["metrics"].get("head_cut")


@needs_postgres
def test_off_ignores_the_order_completely(ingest):
    result, reader = discover(ingest, "off", OLD, reader=StoppableReader)
    assert not reader.asked and not result["metrics"].get("head_cut")


@needs_postgres
def test_a_document_that_contradicts_the_recorded_order_is_read_in_full(ingest):
    # Registrado como newest_first, mas o documento sobe no tempo: nada é cortado.
    ascending = list(range(1, 41))[::-1][::-1]
    result, _ = discover(ingest, "enforce", [-h for h in ascending] + [-5], order="newest_first")
    assert result["metrics"]["read_order_contradicted"] == 1
    assert not result["metrics"].get("head_cut")


@needs_postgres
@pytest.mark.parametrize("value", ["agressivo", True, None, 1])
def test_head_filter_setting_is_validated(ingest, value):
    with pytest.raises(CatalogError):
        ingest.configure(dict(version=ingest.settings()["version"], config=dict(head_filter=value)))


@needs_postgres
def test_head_filter_defaults_to_shadow_and_accepts_every_mode(ingest):
    assert ingest.settings()["config"]["head_filter"] == "shadow"
    for mode in ("off", "shadow", "enforce"):
        ingest.configure(dict(version=ingest.settings()["version"], config=dict(head_filter=mode)))
        assert ingest.settings()["config"]["head_filter"] == mode


@needs_postgres
def test_a_contradicted_label_is_corrected_in_the_catalog(ingest):
    ingest.configure(dict(version=ingest.settings()["version"], config=dict(head_filter="shadow")))
    ingest.create_run({})
    job = next_job(ingest, "discovery")
    link_id = job["snapshot"]["link_id"]
    with ingest.connection() as db:
        db.execute("UPDATE links SET read_order='newest_first' WHERE id=%s", (link_id,))
    job["snapshot"].update(
        model="rss",
        kind="feed",
        parameters=get_mechanism("rss").defaults(),
        read_order="newest_first",
    )
    body = feed(PINNED_OLD_ON_TOP, job["run"]["since"])
    engine = Engine(
        ingest,
        reader_factory=lambda roots, progress: FakeReader({job["payload"]["url"]: body}, progress),
    )
    engine.discovery(job)
    with ingest.connection() as db:
        found = db.execute("SELECT read_order FROM links WHERE id=%s", (link_id,)).fetchone()
    assert found["read_order"] == "unordered"
