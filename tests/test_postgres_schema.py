"""O DDL do Postgres (``sql/postgres``) é a fonte do esquema do estado do buscador."""

import re
from pathlib import Path

from buscador_midias.state.store import StateStore

SQL = Path(__file__).resolve().parents[1] / "sql" / "postgres"
UP = "\n".join(path.read_text(encoding="utf-8") for path in sorted(SQL.glob("00*.sql")))
DOWN = (SQL / "999_drop_midias_crawler_tables.sql").read_text(encoding="utf-8")

TABLES = {
    "midias_crawler_runs",
    "midias_crawler_urls",
    "midias_crawler_site_kind_counts",
    "midias_crawler_sitemaps",
    "midias_crawler_site_baselines",
    "midias_crawler_site_runs",
    "midias_crawler_contents",
    "midias_crawler_analyses",
    "midias_crawler_evidences",
    "midias_crawler_enrich_runs",
    "midias_crawler_corpus_writes",
    "midias_crawler_corpus_runs",
    "midias_crawler_fallback_hosts",
    "midias_crawler_source_strategies",
}


def test_o_ddl_cria_exatamente_as_tabelas_do_estado():
    assert set(re.findall(r"CREATE TABLE (\w+) \(", UP)) == TABLES


def test_as_tabelas_criadas_no_banco_de_teste_sao_as_do_ddl():
    store = StateStore()

    rows = store._all(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )

    assert {row[0] for row in rows} == TABLES


def test_o_down_remove_todas_as_tabelas_em_ordem_inversa_das_dependencias():
    created = re.findall(r"CREATE TABLE (\w+) \(", UP)
    dropped = re.findall(r"DROP TABLE IF EXISTS (\w+);", DOWN)

    assert set(dropped) == set(created)
    position = {name: dropped.index(name) for name in dropped}
    for child, body in re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", UP, re.S):
        for parent in re.findall(r"REFERENCES (\w+) ", body):
            assert position[child] < position[parent], (child, parent)


def test_os_valores_aceitos_pelos_checks_cobrem_o_que_o_codigo_grava():
    from buscador_midias.discovery.explore import ProbeStatus
    from buscador_midias.discovery.sources import SourceKind
    from buscador_midias.state.store import UrlKind

    for value in UrlKind:
        assert f"'{value.value}'" in UP
    for value in ProbeStatus:
        assert f"'{value.value}'" in UP
    for value in SourceKind:
        assert f"'{value.value}'" in UP


def test_os_checks_das_estrategias_cobrem_os_enums_do_dominio():
    from buscador_midias.domain.strategy import CandidateOrigin, Role, StrategyKind, Verdict

    for enum in (CandidateOrigin, Role, StrategyKind, Verdict):
        for value in enum:
            assert f"'{value.value}'" in UP, (enum, value)
