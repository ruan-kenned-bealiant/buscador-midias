# ruff: noqa: F811 -- fixtures importadas de test_ingestion são reutilizadas por nome.
"""Tamanho dos pools: o de checkpoints atende todos os workers; o de publicação é separado."""

from __future__ import annotations

import os

import pytest
from test_catalog_postgres import local, schema  # noqa: F401
from test_ingestion import ingest  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


def test_checkpoint_pool_has_room_for_the_worker_threads(ingest):
    ingest.start_pool()
    try:
        assert ingest.pool.pool.max_size == 24
    finally:
        ingest.close_pool()


def test_publication_pool_stays_small_and_independent(ingest):
    ingest.start_pool()
    try:
        assert ingest.publisher_pool.pool.max_size == 12
        assert ingest.publisher_pool is not ingest.pool
    finally:
        ingest.close_pool()
