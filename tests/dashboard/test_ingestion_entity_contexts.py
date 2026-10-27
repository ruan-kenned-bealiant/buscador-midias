# ruff: noqa: F811 -- fixtures importadas são compartilhadas por nome.
"""Contextos em lotes retomáveis: trinta entidades, resposta parcial e reserva substituída."""

import os
from dataclasses import replace

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import Analyzer, ingest, next_job  # noqa: F401

from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion.engine import Engine, RetryableError

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Postgres isolado"
)


def context_job(repo):
    repo.create_run({})
    repo.finish(next_job(repo, "discovery"), dict(candidates=[dict(url="https://jose.example/n")]))
    repo.finish(next_job(repo, "dedup"), dict(next="fetch"))
    repo.finish(
        next_job(repo, "fetch"), dict(next="context", article=dict(body="Texto", title="N"))
    )
    return next_job(repo, "context")


class ManyEntities(Analyzer):
    def __init__(self):
        self.identifications = 0
        self.batches = []
        self.partial = False

    def analyze(self, items):
        self.identifications += 1
        return [
            replace(
                super().analyze(items)[0],
                entities=tuple(
                    dict(name=f"Pessoa {i:02}", type="PF", role="citado") for i in range(30)
                ),
            )
        ]

    def analyze_entities(self, item, entities):
        self.batches.append([e["name"] for e in entities])
        if self.partial and len(self.batches) == 2:
            return super().analyze_entities(item, entities[:2])
        return super().analyze_entities(item, entities)


def test_trinta_entidades_sao_divididas_em_oito_lotes(ingest):
    job = context_job(ingest)
    analyzer = ManyEntities()
    result = Engine(ingest, analyzer_factory=lambda: analyzer).context(job)
    assert analyzer.identifications == 1
    assert list(map(len, analyzer.batches)) == [4] * 7 + [2]
    assert len(result["article"]["analysis"]["entity_contexts"]) == 30
    assert result["article"]["analysis"]["entity_limit_reached"] is True
    assert result["coverage_partial"] is True


def test_retentativa_reaproveita_identificacao_e_contextos_completos(ingest):
    job = context_job(ingest)
    analyzer = ManyEntities()
    analyzer.partial = True
    engine = Engine(ingest, analyzer_factory=lambda: analyzer)
    with pytest.raises(RetryableError, match="lote pendente"):
        engine.context(job)
    ingest.failure(job, "Lote individual incompleto")
    with ingest.connection() as db:
        db.execute("UPDATE ingest_jobs SET available_at=now() WHERE id=%s", (job["id"],))
    job = next_job(ingest, "context")
    assert job["attempts"] == 2
    assert len(job["article"]["payload"]["entity_context_progress"]["contexts"]) == 6
    analyzer.partial = False
    result = engine.context(job)
    assert analyzer.identifications == 1
    assert analyzer.batches[2][0] == "Pessoa 06"
    assert len(result["article"]["analysis"]["entity_contexts"]) == 30


def test_checkpoint_de_worker_substituido_nao_altera_contextos(ingest):
    job = context_job(ingest)
    with ingest.connection() as db:
        db.execute("UPDATE ingest_jobs SET owner='outro' WHERE id=%s", (job["id"],))
    with pytest.raises(CatalogError):
        ingest.save_entity_context_progress(job, dict(contexts=[]))
    with ingest.connection() as db:
        payload = db.execute("SELECT payload FROM ingest_articles WHERE hash=%s", (job["hash"],))
        assert "entity_context_progress" not in payload.fetchone()["payload"]


def test_mudanca_de_modelo_invalida_checkpoint_sem_reaproveitar_parecer_antigo(ingest):
    job = context_job(ingest)
    analyzer = ManyEntities()
    analyzer.partial = True
    engine = Engine(ingest, analyzer_factory=lambda: analyzer)
    with pytest.raises(RetryableError):
        engine.context(job)
    with ingest.connection() as db:
        payload = db.execute("SELECT payload FROM ingest_articles WHERE hash=%s", (job["hash"],))
        job["article"]["payload"] = payload.fetchone()["payload"]
    job["run"]["config"]["model"] = "model/new-version"
    analyzer.partial = False
    result = engine.context(job)
    assert analyzer.identifications == 2
    assert result["article"]["analysis"]["model"] == "model/new-version"
