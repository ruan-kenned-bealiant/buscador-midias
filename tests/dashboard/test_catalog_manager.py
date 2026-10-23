"""Cadastro real persistente, estados herdados, evidências e API local protegida."""

import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from buscador_midias.catalog.consolidation import (
    consolidate_regionals,
    consolidate_reviewed,
    regional_plan,
)
from buscador_midias.catalog.review import audit_catalog, reviewed_plan
from buscador_midias.catalog.store import CatalogError, CatalogStore, clean_url
from buscador_midias.dashboard.server import make_server


@pytest.fixture
def seed(tmp_path):
    sites = tmp_path / "sites.csv"
    sites.write_text(
        "region,name,url,country,country_code,language,source_type\n"
        "América,Fonte A,https://a.example,Brasil,BR,Português,portal\n"
        "América,Fonte B,https://b.example,Brasil,BR,Português,portal\n"
    )
    snapshot = tmp_path / "retrato.json"
    snapshot.write_text(
        json.dumps(
            dict(
                meta=dict(generated_at="2026-10-20T00:00:00Z"),
                sources=[
                    dict(key="a.example", name="Fonte A", country="Brasil"),
                    dict(key="b.example", name="Fonte B", country="Brasil"),
                ],
                mechanisms={
                    "a.example": [
                        dict(
                            url="https://a.example/feed/",
                            kind="feed",
                            texts=2,
                            listed=30,
                            observed_at="2026-10-20T00:00:00Z",
                            evidence="xml",
                        ),
                        dict(url="https://a.example/podcast.xml", kind="feed", audio=True),
                    ]
                },
            )
        )
    )
    return tmp_path / "catalogo.sqlite", snapshot, sites


@pytest.fixture
def store(seed):
    return CatalogStore(*seed)


def new_source(store, **extra):
    return store.save("source", dict(name="Novo site", url="https://novo.example", **extra))


def test_import_is_once_preserves_history_and_does_not_resurrect(seed, store):
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in seed[1:]}
    assert store.summary()["sources"] == dict(total=2, active=2)
    assert store.summary()["endpoints"] == dict(total=2, active=1)
    source = store.sources()["items"][0]
    store.delete("source", source["id"], source["version"])
    reopened = CatalogStore(*seed)
    assert reopened.summary()["sources"]["total"] == 1
    assert reopened.summary()["endpoints"]["total"] == 0
    assert {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in seed[1:]} == hashes
    with sqlite3.connect(seed[0]) as db:
        assert db.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM events WHERE action='import'").fetchone()[0] == 1


def test_source_inactivation_suspends_without_changing_link_choices(store):
    source = store.sources()["items"][0]
    paused = store.save("source", dict(active=False, version=source["version"]), source["id"])
    rows = store.endpoints(source["id"])["items"]
    assert all(not row["effective_active"] for row in rows)
    assert [r["active"] for r in rows] == [True, False]
    store.save("source", dict(active=True, version=paused["version"]), source["id"])
    assert [r["effective_active"] for r in store.endpoints(source["id"])["items"]] == [True, False]


def test_url_edit_resets_evidence_but_label_edit_preserves_it(store):
    row = store.endpoints(store.sources()["items"][0]["id"])["items"][0]
    original = row["evidence"]
    edited = store.save("endpoint", dict(label="Política", version=row["version"]), row["id"])
    assert json.loads(edited["evidence"]) == original
    changed = store.save(
        "endpoint",
        dict(url="https://a.example/new.xml", kind="sitemap", version=edited["version"]),
        edited["id"],
    )
    assert json.loads(changed["evidence"]) == {}
    with store.connection() as db:
        old = db.execute(
            "SELECT before_json FROM events WHERE entity='endpoint' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert json.loads(json.loads(old)["evidence"]) == original


def test_creation_duplicate_and_update_conflicts_are_atomic(store):
    row = new_source(store)
    with pytest.raises(CatalogError) as duplicate:
        store.save("source", dict(name="Duplicata", url="http://www.novo.example/"))
    assert duplicate.value.status == 409
    store.save("source", dict(name="Renomeado", version=1), row["id"])
    with pytest.raises(CatalogError) as stale:
        store.save("source", dict(name="Sobrescrita", version=1), row["id"])
    assert stale.value.status == 409
    assert store.source(row["id"])["name"] == "Renomeado"
    with pytest.raises(CatalogError):
        store.delete("source", row["id"], 1)
    assert store.source(row["id"])["version"] == 2


def test_two_writers_cannot_overwrite_one_revision(store):
    row = new_source(store)

    def edit(name):
        try:
            return store.save("source", dict(name=name, version=row["version"]), row["id"])["name"]
        except CatalogError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["A", "B"]))
    assert results.count(409) == 1
    assert store.source(row["id"])["version"] == 2


def test_search_accepts_accents_and_uppercase(store):
    store.save("source", dict(name="São Luís", url="https://saoluis.example"))
    assert store.sources(q="SAO LUIS")["total"] == 1


def test_source_and_link_crud_pagination_and_filters(store):
    row = new_source(store, country="Portugal", active=False)
    link = store.save(
        "endpoint",
        dict(url="https://novo.example/sitemap.xml", kind="sitemap"),
        source_id=row["id"],
    )
    assert store.endpoints(row["id"])["items"][0]["evidence"] == {}
    assert not store.endpoints(row["id"])["items"][0]["effective_active"]
    assert store.sources(country="Portugal", status="inactive")["total"] == 1
    assert store.sources(q="novo.example", offset=1)["items"] == []
    assert store.endpoints(row["id"], kind="feed")["total"] == 0
    with pytest.raises(CatalogError) as duplicate:
        store.save("endpoint", dict(url=link["url"], kind="feed"), source_id=row["id"])
    assert duplicate.value.status == 409
    store.delete("endpoint", link["id"], link["version"])
    assert store.endpoints(row["id"])["total"] == 0
    store.delete("source", row["id"], row["version"])
    with pytest.raises(CatalogError) as gone:
        store.endpoints(row["id"])
    assert gone.value.status == 404


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pass@site.example",
        "http://127.0.0.1/feed",
        "http://localhost/feed",
        "https://a.local/rss",
        "https://10.0.0.1/rss",
        "https://site.example:wrong/rss",
        "https://site.example/a b",
        "https://site.example\\evil/rss",
        "https://-bad.example/rss",
    ],
)
def test_invalid_or_private_urls_rejected(url):
    with pytest.raises(CatalogError):
        clean_url(url)


def test_bad_kind_and_boolean_do_not_create_partial_records(store):
    row = new_source(store)
    for data in [
        dict(url="https://novo.example/feed", kind="html"),
        dict(url="https://novo.example/feed", kind="feed", active="false"),
    ]:
        with pytest.raises(CatalogError):
            store.save("endpoint", data, source_id=row["id"])
    assert store.endpoints(row["id"])["total"] == 0


@pytest.fixture
def manager(seed):
    path, snapshot, sites = seed

    def no_state():
        raise AssertionError("Gerenciador não acessa o banco de coleta")

    server = make_server(
        no_state,
        "127.0.0.1",
        0,
        sites_path=sites,
        functional_directory=snapshot.parent,
        manager_db_path=path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(5)


def request(base, path, method="GET", data=None, headers=None):
    headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(
        base + path,
        method=method,
        headers=headers,
        data=json.dumps(data).encode() if data is not None else None,
    )
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw.startswith(b"{") else raw)


def test_api_crud_no_state_and_protected_writes(manager):
    prefix = "/api/gerenciador"
    assert request(manager, "/gerenciador")[0] == 200
    _, summary = request(manager, prefix + "/resumo")
    headers = {"X-Catalog-Token": summary["csrf_token"]}
    data = dict(name="<script>Nome seguro</script>", url="https://novo.example")
    assert request(manager, prefix + "/fontes", "POST", data)[0] == 403
    assert (
        request(
            manager, prefix + "/fontes", "POST", data, {**headers, "Origin": "https://evil.example"}
        )[0]
        == 403
    )
    assert (
        request(manager, prefix + "/fontes", "POST", data, {**headers, "Host": "evil.example"})[0]
        == 403
    )
    code, source = request(manager, prefix + "/fontes", "POST", data, headers)
    assert code == 201
    code, link = request(
        manager,
        prefix + f"/fontes/{source['id']}/mecanismos",
        "POST",
        dict(url="https://novo.example/feed/", kind="feed"),
        headers,
    )
    assert code == 201
    code, updated = request(
        manager,
        prefix + f"/mecanismos/{link['id']}",
        "PATCH",
        dict(active=False, version=1),
        headers,
    )
    assert code == 200 and updated["active"] == 0
    assert (
        request(manager, prefix + f"/mecanismos/{link['id']}", "DELETE", dict(version=1), headers)[
            0
        ]
        == 409
    )
    assert (
        request(manager, prefix + f"/mecanismos/{link['id']}", "DELETE", dict(version=2), headers)[
            0
        ]
        == 200
    )
    assert (
        request(manager, prefix + f"/fontes/{source['id']}", "DELETE", dict(version=1), headers)[0]
        == 200
    )
    assert request(manager, prefix + f"/fontes/{source['id']}")[0] == 404
    # Os endpoints antigos continuam sem permissão de escrita.
    assert request(manager, "/api/funcionais", "POST", data, headers)[0] == 405
    assert request(manager, prefix + "/fontes?limit=1000")[0] == 400
    assert request(manager, prefix + "/fontes?status=invalid")[0] == 400
    assert request(manager, prefix + "/fontes", "POST", [], headers)[0] == 400
    assert (
        request(manager, prefix + "/fontes", "POST", dict(**data, evidence=True), headers)[0] == 422
    )


def test_model_configuration_api_is_protected_and_persistent(manager):
    prefix = "/api/gerenciador"
    assert request(manager, "/mecanismos")[0] == 200
    _, summary = request(manager, prefix + "/resumo")
    headers = {"X-Catalog-Token": summary["csrf_token"]}
    _, models = request(manager, prefix + "/modelos")
    assert len(models["items"]) == 5
    rss = next(m for m in models["items"] if m["id"] == "rss")
    assert "max_depth" not in rss["parameters"]
    body = dict(version=rss["version"], parameters=dict(article_samples=1))
    assert request(manager, prefix + "/modelos/rss", "PATCH", body)[0] == 403
    code, saved = request(manager, prefix + "/modelos/rss", "PATCH", body, headers)
    assert code == 200 and saved["parameters"]["article_samples"] == 1 and saved["version"] == 2
    assert request(manager, prefix + "/modelos/rss", "PATCH", body, headers)[0] == 409
    assert (
        request(
            manager,
            prefix + "/modelos/rss",
            "PATCH",
            dict(version=2, parameters=dict(max_depth=11)),
            headers,
        )[0]
        == 422
    )
    _, actual = request(manager, prefix + "/modelos/rss")
    assert actual["parameters"]["article_samples"] == 1


@pytest.fixture
def regional_catalog(store):
    parent = store.save("source", dict(name="g1", url="https://g1.globo.com"))
    child = store.save("source", dict(name="g1 Acre", url="https://g1.globo.com/ac/acre"))
    store.save("source", dict(name="GloboNews", url="https://g1.globo.com/globonews"))
    shared = "https://g1.globo.com/rss/g1/ac/acre/"
    for source in (parent, child):
        store.save("endpoint", dict(url=shared, kind="feed", model="rss"), source_id=source["id"])
    unique = store.save(
        "endpoint",
        dict(url="https://g1.globo.com/ac.xml", kind="sitemap", active=False),
        source_id=child["id"],
    )
    with store.connection() as db:
        db.execute(
            "UPDATE links SET evidence=?,last_test=? WHERE id=?",
            ('{"texts":1}', '{"outcome":"text_confirmed"}', unique["id"]),
        )
    return parent, child, unique


def test_regional_consolidation_preserves_links_history_and_other_brands(store, regional_catalog):
    parent, child, unique = regional_catalog
    plan = regional_plan(store)
    assert [item["source_id"] for item in plan] == [child["id"]]
    result = consolidate_regionals(store, plan)
    assert result == dict(sources=1, added_links=1, shared_links=1, labeled_links=1)
    assert regional_plan(store) == []
    rows = store.endpoints(parent["id"])["items"]
    assert len(rows) == 2
    assert next(row for row in rows if row["kind"] == "feed")["label"] == "g1 Acre"
    copied = next(row for row in rows if row["kind"] == "sitemap")
    assert copied["evidence"] == {"texts": 1} and not copied["active"]
    assert copied["last_test"] == {}  # A prova de outra configuração fica na origem.
    with store.connection() as db:
        old = db.execute("SELECT * FROM links WHERE id=?", (unique["id"],)).fetchone()
        assert old["evidence"] == '{"texts":1}'
        assert old["last_test"] == '{"outcome":"text_confirmed"}'
        event = db.execute(
            "SELECT after_json FROM events WHERE action='consolidate' AND entity='source'"
        ).fetchone()
        assert json.loads(event[0])["consolidated_into"] == parent["id"]
    assert any(row["name"] == "GloboNews" for row in store.sources()["items"])
    assert consolidate_regionals(store, []) == dict(
        sources=0, added_links=0, shared_links=0, labeled_links=0
    )


def test_regional_consolidation_rejects_stale_plan(store, regional_catalog):
    parent, child, _ = regional_catalog
    plan = regional_plan(store)
    store.save("source", dict(version=child["version"], notes="Edição concorrente"), child["id"])
    with pytest.raises(CatalogError, match="catálogo mudou"):
        consolidate_regionals(store, plan)
    assert store.source(child["id"])["notes"] == "Edição concorrente"
    assert store.endpoints(parent["id"])["total"] == 1


def test_regional_consolidation_rolls_back_on_deleted_target_link(store, regional_catalog):
    parent, child, _ = regional_catalog
    endpoint = store.endpoints(parent["id"])["items"][0]
    store.delete("endpoint", endpoint["id"], endpoint["version"])
    with pytest.raises(CatalogError, match="foi excluído"):
        consolidate_regionals(store, regional_plan(store))
    assert store.source(child["id"])["deleted_at"] is None
    assert store.endpoints(child["id"])["total"] == 2


@pytest.fixture
def reviewed_catalog(store):
    parent = store.save("source", dict(name="Folha de S.Paulo", url="https://www.folha.uol.com.br"))
    child = store.save("source", dict(name="Folha de S.Paulo", url="https://www1.folha.uol.com.br"))
    endpoint = store.save(
        "endpoint", dict(url=child["url"] + "/sitemap.xml", kind="sitemap"), source_id=child["id"]
    )
    return parent, child, endpoint


def test_whole_catalog_review_finds_alias_and_preserves_all_urls(store, reviewed_catalog):
    parent, child, _ = reviewed_catalog
    before = audit_catalog(store)
    assert before["sources"] == 4 and before["endpoints"] == 3
    assert any(parent["id"] in ids and child["id"] in ids for ids in before["same_names"])
    assert before["integrity"] == ["ok"] and not before["foreign_key_violations"]
    result = consolidate_reviewed(store, reviewed_plan(store))
    assert result["sources"] == result["added_links"] == 1
    after = audit_catalog(store)
    assert after["sources"] == 3 and after["unique_endpoint_urls"] == before["unique_endpoint_urls"]
    assert child["name"] in store.source(parent["id"])["notes"]
    assert consolidate_reviewed(store, reviewed_plan(store))["sources"] == 0


def test_reviewed_plan_rejects_concurrent_endpoint_edit(store, reviewed_catalog):
    _, child, endpoint = reviewed_catalog
    plan = reviewed_plan(store)
    store.save("endpoint", dict(version=endpoint["version"], active=False), endpoint["id"])
    with pytest.raises(CatalogError, match="catálogo mudou"):
        consolidate_reviewed(store, plan)
    assert store.source(child["id"])["deleted_at"] is None


def test_review_does_not_merge_homonyms_or_independent_brands(store):
    for name, url in [
        ("The Reporter", "https://reporter.example"),
        ("The Reporter", "https://other.example"),
        ("g1", "https://g1.globo.com"),
        ("GloboNews", "https://g1.globo.com/globonews"),
    ]:
        store.save("source", dict(name=name, url=url))
    audit = audit_catalog(store)
    assert len(audit["same_names"]) == 1 and len(audit["shared_hosts"]) == 1
    assert reviewed_plan(store) == []


def test_audit_detects_invalid_data_and_external_hosts_without_marking_fetch_failure(store):
    source = new_source(store)
    endpoint = store.save(
        "endpoint", dict(url="https://feeds.example/feed", kind="feed"), source_id=source["id"]
    )
    with store.connection() as db:
        db.execute("UPDATE links SET model='sitemap' WHERE id=?", (endpoint["id"],))
    audit = audit_catalog(store)
    assert endpoint["id"] in audit["incompatible_kind_model"]
    assert endpoint["id"] in audit["without_xml_evidence"]
    assert audit["external_endpoint_hosts"][0]["id"] == endpoint["id"]


def test_url_repair_preserves_old_proof_and_does_not_invent_functionality(store):
    from buscador_midias.catalog.review import repair_malformed_links, repair_url_plan

    source = new_source(store)
    link = store.save(
        "endpoint", dict(url="https://novo.example/map.xml", kind="sitemap"), source_id=source["id"]
    )
    with store.connection() as db:
        db.execute(
            "UPDATE links SET url=?,evidence=? WHERE id=?",
            ("https://novo.example/Gardening Australia.xml", '{"texts":1}', link["id"]),
        )
    plan = repair_url_plan(store)
    assert plan[0]["new_url"] == "https://novo.example/Gardening%20Australia.xml"
    assert repair_malformed_links(store, plan) == 1
    assert store.endpoint(link["id"])["evidence"] == {}
    assert audit_catalog(store)["invalid_records"] == []
    with store.connection() as db:
        old = json.loads(
            db.execute("SELECT before_json FROM events WHERE action='repair_url'").fetchone()[0]
        )
        assert json.loads(old["evidence"]) == {"texts": 1}
        assert " " in old["url"]
    assert repair_url_plan(store) == []
