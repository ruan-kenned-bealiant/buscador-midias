"""Opções de sessão das conexões do buscador: limites que protegem o banco compartilhado."""

from __future__ import annotations

import pytest

from buscador_midias.catalog.postgres import postgres_parameters
from buscador_midias.catalog.store import CatalogError

ENV = {
    "DB_MIDIAS_IA_HOST": "127.0.0.1",
    "DB_MIDIAS_IA_PORT": "5432",
    "DB_MIDIAS_IA_DATABASE": "midias",
    "DB_MIDIAS_IA_USERNAME": "usuario",
    "DB_MIDIAS_IA_PASSWORD": "segredo",
}


def test_sessions_never_use_parallel_workers():
    # Contêiner com /dev/shm de 64 MB: consultas paralelas falhavam com DiskFull
    # ("could not resize shared memory segment") e derrubavam ciclos do publicador.
    assert "max_parallel_workers_per_gather=0" in postgres_parameters(ENV)["options"]


def test_sessions_keep_the_existing_safety_limits():
    options = postgres_parameters(ENV)["options"]
    for expected in ("timezone=UTC", "lock_timeout=15000", "statement_timeout=120000"):
        assert expected in options


@pytest.mark.parametrize("missing", sorted(ENV))
def test_missing_credentials_are_rejected(missing):
    with pytest.raises(CatalogError):
        postgres_parameters({k: v for k, v in ENV.items() if k != missing})
