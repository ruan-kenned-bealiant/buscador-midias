"""Identidade, limites e preservação de decisões manuais na correção de cobertura."""

import gzip
import importlib
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.postgres_schema import DDL
from buscador_midias.domain.site import Site
from buscador_midias.domain.url import url_hash_candidates
from buscador_midias.ingestion.schema import DDL as INGEST_DDL

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
routes = importlib.import_module("coverage_gap_routes")


def test_candidate_routes_are_bounded_official_and_not_guessed_state_paths(tmp_path):
    source = Site.create(
        name="Teste",
        url="https://news.example",
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )
    body = (
        '<link rel="alternate" type="application/rss+xml" href="/official.rss">'
        '<a href="https://external.example/feed">RSS</a>'
        + "".join(f'<a href="/rss/{n}">RSS</a>' for n in range(20))
    ).encode()
    (tmp_path / "home.gz").write_bytes(gzip.compress(body))
    found = routes.route_candidates(
        dict(reference_pages=[dict(url=source.url, proof="home.gz")]), tmp_path, source
    )
    assert len(found) == 8
    assert found[0] == "https://news.example/official.rss"
    assert all(source.owns(url) for url in found)


def test_feed_must_contain_the_missing_url_not_just_a_similar_headline():
    address = "https://news.example/story"
    doc = parse_document(
        f'<rss version="2.0"><channel><title>Notícias</title><item><title>Noticia</title>'
        f"<link>{address}?utm_source=rss</link></item></channel></rss>".encode(),
        "https://news.example/feed",
    )
    assert routes.matching_entries(
        doc, [dict(url=address, hashes=url_hash_candidates(address))]
    ) == [
        address,
    ]
    assert routes.matching_entries(doc, [dict(url=address, hashes=["different"])]) == []


def test_unproven_or_wrong_configuration_never_opens_a_write_connection(monkeypatch):
    monkeypatch.setattr(routes, "connect_postgres", lambda: pytest.fail("Sem prova não grava"))
    assert (
        routes.add_verified_route(
            dict(id=1, url="https://news.example"),
            dict(url="https://news.example/feed", kind="feed", transport="direct"),
            {},
            {},
        )["decision"]
        == "proof_or_configuration_mismatch"
    )


@pytest.fixture
def correction_database(pg_server, monkeypatch):
    """Esquema descartável no banco exclusivo de testes; nunca toca no catálogo real."""
    if pg_server is None:
        pytest.skip("PostgreSQL de teste indisponível")
    import uuid

    import psycopg
    from psycopg import sql

    schema = "coverage_" + uuid.uuid4().hex
    with psycopg.connect(**pg_server) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        db.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        db.execute(DDL)
        db.execute(INGEST_DDL.split(";")[0])
        db.execute("INSERT INTO mechanisms VALUES('rss','{}',1,now())")
        db.execute(
            "INSERT INTO sites(id,identity,name,url,created_at,updated_at) "
            "VALUES(1,'news.example','Teste','https://news.example',now(),now())"
        )

    class Connection:
        def __init__(self, db):
            self.db = db

        @property
        def row_factory(self):
            return self.db.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.db.row_factory = value

        def execute(self, query, params=()):
            return self.db.execute(query.replace("midias_manager.", schema + "."), params)

    @contextmanager
    def connect():
        with psycopg.connect(**pg_server) as db:
            yield Connection(db)

    monkeypatch.setattr(routes, "connect_postgres", connect)
    yield connect
    with psycopg.connect(**pg_server) as db:
        db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_verified_new_route_is_atomic_and_replay_preserves_existing(correction_database):
    source = dict(id=1, name="Teste", url="https://news.example", version=1)
    route = dict(url="https://news.example/feed", kind="feed", transport="direct")
    test = dict(
        usable_for_news=True,
        outcome="text_confirmed",
        source_id=1,
        source_url=source["url"],
        url=route["url"],
        requested_kind="feed",
        model="rss",
        model_version=1,
        access_transport="direct",
        test_id="proof",
        parameters={},
        root_entries=1,
        root_children=0,
        root_index=False,
        text_samples=1,
        observed_at="2026-10-26T22:00:00Z",
        samples=[],
    )
    proof = dict(matched_urls=[source["url"] + "/story"])
    result = routes.add_verified_route(source, route, test, proof)
    assert result["decision"] == "created"
    assert (
        routes.add_verified_route(source, route, test, proof)["decision"]
        == "existing_route_preserved"
    )
    with correction_database() as db:
        assert db.execute("SELECT count(*) n FROM midias_manager.links").fetchone()[0] == 1
        assert (
            db.execute("SELECT count(*) n FROM midias_manager.link_test_results").fetchone()[0] == 1
        )
        assert db.execute("SELECT count(*) n FROM midias_manager.events").fetchone()[0] == 1
        assert (
            db.execute("SELECT purpose FROM midias_manager.ingest_daily_routes").fetchone()[0]
            == "updates"
        )
        db.execute("UPDATE midias_manager.sites SET active=0 WHERE id=1")
    assert (
        routes.add_verified_route(source, route, test, proof)["decision"]
        == "preserved_inactive_source"
    )
