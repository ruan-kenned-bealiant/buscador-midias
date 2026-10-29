# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Corte pelo topo na descoberta: sombra só mede, ativo dispensa o documento inteiro."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_catalog_testing import FakeReader
from test_ingestion import ingest, next_job  # noqa: F401

from buscador_midias.catalog.head_probe import prefix_outside
from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion import engine as engine_module
from buscador_midias.ingestion.engine import Engine
from buscador_midias.ingestion.flow import audit_sampled
from buscador_midias.net.fetcher import FetchResult

needs_postgres = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def feed(ages_in_hours, since):
    """Feed com um item por idade (horas antes do início da janela; negativa = dentro dela)."""
    # Minutos fora do zero: nenhum item cai na meia-noite exata, que vale "o dia inteiro".
    stamp = "%a, %d %b %Y %H:%M:%S +0000"
    items = "".join(
        f"<item><link>https://jose.example/n{i}</link>"
        f"<pubDate>{(since - timedelta(hours=h, minutes=7)).strftime(stamp)}</pubDate></item>"
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


def discover(ingest, mode, ages, *, order="newest_first", reader=FakeReader, **settings):
    ingest.configure(
        dict(
            version=ingest.settings()["version"],
            config=dict(
                window_mode="today",
                head_filter=mode,
                lastmod_filter="off",
                url_filter="off",
                **settings,
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


@needs_postgres
def test_an_undated_entry_in_a_labelled_document_blocks_the_cut_and_demotes_the_label(ingest):
    ingest.configure(dict(version=ingest.settings()["version"], config=dict(head_filter="enforce")))
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
    since = job["run"]["since"]
    body = feed(OLD, since).replace(
        b"</channel>", b"<item><link>https://jose.example/donate</link></item></channel>"
    )
    engine = Engine(
        ingest,
        reader_factory=lambda roots, progress: FakeReader({job["payload"]["url"]: body}, progress),
    )
    result = engine.discovery(job)
    assert result["metrics"]["read_order_contradicted"] == 1
    assert not result["metrics"].get("head_cut")
    assert "donate" in " ".join(c["url"] for c in result["candidates"])
    with ingest.connection() as db:
        found = db.execute("SELECT read_order FROM links WHERE id=%s", (link_id,)).fetchone()
    assert found["read_order"] == "unknown"


def test_audit_sampling_is_deterministic_and_close_to_the_rate():
    assert not audit_sampled("run", 1, 0) and audit_sampled("run", 1, 1)
    assert audit_sampled("run-a", 7, 0.5) == audit_sampled("run-a", 7, 0.5)
    hits = sum(audit_sampled("run-a", link, 0.02) for link in range(20000))
    assert 300 <= hits <= 500  # 2% de 20.000, com folga estatística
    assert {audit_sampled("run-a", link, 0.5) for link in range(50)} == {True, False}


@needs_postgres
def test_audit_reads_the_whole_document_and_reports_what_the_cut_would_have_done(ingest):
    result, reader = discover(ingest, "enforce", OLD, reader=StoppableReader, head_audit_rate=1)
    assert not reader.asked  # sem parada antecipada: o documento chegou inteiro
    assert result["metrics"]["head_audit_sampled"] == 1
    assert result["metrics"]["head_shadow_would_skip"] == 1
    assert result["metrics"]["head_shadow_missed"] == 0
    assert "head_cut" not in result["metrics"]


@needs_postgres
def test_a_link_outside_the_sample_is_cut_exactly_as_before(ingest):
    result, reader = discover(ingest, "enforce", OLD, reader=StoppableReader, head_audit_rate=0)
    assert reader.asked and result["metrics"]["head_stopped_download"] == 1
    assert "head_audit_sampled" not in result["metrics"]


@needs_postgres
def test_audit_does_not_apply_when_the_cut_is_not_enforced(ingest):
    result, _ = discover(ingest, "shadow", OLD, head_audit_rate=1)
    assert "head_audit_sampled" not in result["metrics"]


@needs_postgres
def test_audit_catches_a_loss_the_early_stop_would_have_caused(ingest, monkeypatch):
    # Três itens antigos e em ordem no começo e notícias da janela só depois: o prefixo parece
    # morto, mas o documento tem o que importa. É exatamente a perda que a amostra procura.
    ages = [30, 31, 32, 33, -3, -2, -1, 1, 2, 3]
    since = datetime.now(ZoneInfo("America/Sao_Paulo")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    body = feed(ages, since)
    step = next(n for n in range(100, len(body)) if prefix_outside(body[:n], since))
    assert step < len(body)
    monkeypatch.setattr(engine_module, "EARLY_STOP_STEPS", (step,))
    result, _ = discover(ingest, "enforce", ages, reader=StoppableReader, head_audit_rate=1)
    assert result["metrics"]["head_audit_sampled"] == 1
    assert result["metrics"]["head_shadow_would_skip"] == 1
    assert result["metrics"]["head_shadow_missed"] == 1


@pytest.mark.parametrize("value", [-0.1, 1.5, True, "0.02", None])
def test_audit_rate_is_validated(ingest, value):
    with pytest.raises(CatalogError):
        ingest.configure(
            dict(version=ingest.settings()["version"], config=dict(head_audit_rate=value))
        )
