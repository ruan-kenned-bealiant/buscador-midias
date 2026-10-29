"""Repositório do catálogo nas tabelas ``bm_*``: mesmo contrato do gerenciador, desenho novo."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.catalog import CatalogRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)


@pytest.fixture
def repo(schema_name: str) -> CatalogRepository:
    repository = CatalogRepository(schema_name)
    repository.install()
    return repository


def new_site(repo: CatalogRepository, name: str = "Fonte A", url: str = "https://a.example", **x):
    return repo.save("source", dict(name=name, url=url, **x))


def new_link(repo: CatalogRepository, site: dict[str, Any], url: str, kind: str = "feed", **x):
    return repo.save("endpoint", dict(url=url, kind=kind, **x), source_id=site["id"])


def proof(repo: CatalogRepository, link_id: int, **overrides: Any) -> dict[str, Any]:
    """Resultado de teste compatível com a configuração atual do link."""
    link = repo.endpoint(link_id)
    site = repo.source(link["source_id"])
    result = dict(
        model_version=link["current_model_version"],
        url=link["url"],
        model=link["model"],
        source_id=link["source_id"],
        requested_kind=link["kind"],
        source_url=site["url"],
        access_transport=link["access_transport"],
        xml_valid=True,
        compatible=True,
        read_order="newest_first",
        root_entries=30,
        root_children=0,
        root_index=False,
        observed_at="2026-10-29T12:00:00Z",
        text_samples=2,
        samples=[dict(url="https://a.example/n1", text=True)],
        outcome="text_confirmed",
        test_id="t-" + str(link_id),
    )
    return {**result, **overrides}


def test_summary_counts_visible_sites_links_and_countries(repo):
    site = new_site(repo, country="Brasil")
    new_link(repo, site, "https://a.example/feed")
    new_link(repo, site, "https://a.example/podcast", active=False)
    other = new_site(repo, "Fonte B", "https://b.example", country="Portugal")
    repo.delete("source", other["id"], other["version"])
    summary = repo.summary()
    assert summary["sources"] == dict(total=1, active=1)
    assert summary["endpoints"] == dict(total=2, active=1)
    assert summary["countries"] == ["Brasil"]
    assert summary["seed"]["sources"] == 2


def test_source_inactivation_suspends_without_changing_link_choices(repo):
    site = new_site(repo)
    new_link(repo, site, "https://a.example/feed")
    new_link(repo, site, "https://a.example/podcast", active=False)
    paused = repo.save("source", dict(active=False, version=site["version"]), site["id"])
    rows = repo.endpoints(site["id"])["items"]
    assert all(not row["effective_active"] for row in rows)
    assert sorted(r["active"] for r in rows) == [False, True]
    repo.save("source", dict(active=True, version=paused["version"]), site["id"])
    assert sorted(r["effective_active"] for r in repo.endpoints(site["id"])["items"]) == [
        False,
        True,
    ]


def test_new_link_defaults_use_the_contract_names(repo):
    site = new_site(repo)
    link = new_link(repo, site, "https://a.example/feed")
    assert link["model"] == "auto"
    assert link["source_id"] == site["id"]
    assert link["search_interval_seconds"] == 14400
    assert link["access_transport"] == "direct"
    assert link["read_order"] == "unknown"
    assert link["evidence"] == {} and link["last_test"] == {}


def test_url_edit_resets_proof_but_label_edit_preserves_it(repo):
    site = new_site(repo)
    link = new_link(repo, site, "https://a.example/feed")
    repo.record_test(link["id"], link["version"], proof(repo, link["id"]))
    row = repo.endpoint(link["id"])
    assert row["evidence"]["texts"] == 2 and row["test_config_current"]
    edited = repo.save("endpoint", dict(label="Política", version=row["version"]), row["id"])
    assert edited["evidence"] == row["evidence"]
    changed = repo.save(
        "endpoint",
        dict(url="https://a.example/new.xml", kind="sitemap", version=edited["version"]),
        row["id"],
    )
    assert changed["evidence"] == {} and changed["last_test"] == {}
    assert changed["read_order"] == "unknown"
    assert not repo.endpoint(row["id"])["test_config_current"]


def test_events_store_only_the_changed_fields(repo):
    site = new_site(repo)
    link = new_link(repo, site, "https://a.example/feed")
    repo.save("endpoint", dict(label="Política", version=link["version"]), link["id"])
    with repo.connection() as db:
        event = db.execute(
            "SELECT entity,action,changes FROM bm_events WHERE entity='endpoint' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert (event["entity"], event["action"]) == ("endpoint", "update")
    changes = event["changes"]
    assert changes["label"] == ["", "Política"]
    assert "evidence" not in changes and "url" not in changes


def test_creation_duplicate_and_update_conflicts_are_atomic(repo):
    site = new_site(repo, "Novo site", "https://novo.example")
    with pytest.raises(CatalogError) as duplicate:
        repo.save("source", dict(name="Duplicata", url="http://www.novo.example/"))
    assert duplicate.value.status == 409
    repo.save("source", dict(name="Renomeado", version=1), site["id"])
    with pytest.raises(CatalogError) as stale:
        repo.save("source", dict(name="Sobrescrita", version=1), site["id"])
    assert stale.value.status == 409
    assert repo.source(site["id"])["name"] == "Renomeado"
    with pytest.raises(CatalogError):
        repo.delete("source", site["id"], 1)
    assert repo.source(site["id"])["version"] == 2


def test_two_writers_cannot_overwrite_one_revision(repo):
    site = new_site(repo)

    def edit(name: str) -> Any:
        try:
            return repo.save("source", dict(name=name, version=site["version"]), site["id"])["name"]
        except CatalogError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["A", "B"]))
    assert results.count(409) == 1
    assert repo.source(site["id"])["version"] == 2


def test_search_accepts_accents_and_uppercase(repo):
    new_site(repo, "São Luís", "https://saoluis.example")
    assert repo.sources(q="SAO LUIS")["total"] == 1
    assert repo.sources(q="saoluis.example")["total"] == 1
    assert repo.sources(q="inexistente")["total"] == 0


def test_crud_pagination_filters_and_logical_deletion(repo):
    site = new_site(repo, "Novo site", "https://novo.example", country="Portugal", active=False)
    link = new_link(repo, site, "https://novo.example/sitemap.xml", "sitemap")
    item = repo.endpoints(site["id"])["items"][0]
    assert item["evidence"] == {} and not item["effective_active"]
    assert repo.sources(country="Portugal", status="inactive")["total"] == 1
    assert repo.sources(q="novo.example", offset=1)["items"] == []
    assert repo.endpoints(site["id"], kind="feed")["total"] == 0
    with pytest.raises(CatalogError) as duplicate:
        new_link(repo, site, link["url"])
    assert duplicate.value.status == 409
    repo.delete("endpoint", link["id"], link["version"])
    assert repo.endpoints(site["id"])["total"] == 0
    # exclusão lógica continua ocupando a chave: reativar por criação é conflito
    with pytest.raises(CatalogError) as removed:
        new_link(repo, site, link["url"], "sitemap")
    assert removed.value.status == 409
    repo.delete("source", site["id"], site["version"])
    with pytest.raises(CatalogError) as gone:
        repo.endpoints(site["id"])
    assert gone.value.status == 404


def test_deleting_a_link_records_who_deactivated_it(repo):
    site = new_site(repo)
    link = new_link(repo, site, "https://a.example/feed")
    repo.delete("endpoint", link["id"], link["version"])
    with repo.connection() as db:
        row = db.execute(
            "SELECT active,deactivated_by,deactivated_at IS NOT NULL AS has_at,"
            "deleted_at IS NOT NULL AS deleted FROM bm_links WHERE id=%s",
            (link["id"],),
        ).fetchone()
    assert row == dict(active=0, deactivated_by="user", has_at=True, deleted=True)


def test_bad_kind_and_boolean_do_not_create_partial_records(repo):
    site = new_site(repo)
    for data in (
        dict(url="https://a.example/feed", kind="html"),
        dict(url="https://a.example/feed", kind="feed", active="false"),
        dict(url="https://a.example/feed", kind="feed", surprise=1),
    ):
        with pytest.raises(CatalogError):
            repo.save("endpoint", data, source_id=site["id"])
    assert repo.endpoints(site["id"])["total"] == 0


def test_recording_a_test_does_not_bump_the_link_version(repo):
    site = new_site(repo)
    link = new_link(repo, site, "https://a.example/feed")
    version = repo.record_test(link["id"], link["version"], proof(repo, link["id"]))
    assert version == link["version"]
    row = repo.endpoint(link["id"])
    assert row["version"] == link["version"]
    assert row["read_order"] == "newest_first"
    assert row["last_test"]["test_id"] == "t-" + str(link["id"])


def test_a_test_for_an_outdated_revision_never_overwrites_a_later_edit(repo):
    site = new_site(repo)
    link = new_link(repo, site, "https://a.example/feed")
    repo.save("endpoint", dict(label="mudou", version=link["version"]), link["id"])
    with pytest.raises(CatalogError) as stale:
        repo.record_test(link["id"], link["version"], proof(repo, link["id"]))
    assert stale.value.status == 409
    assert repo.endpoint(link["id"])["last_test"] == {}


@pytest.mark.parametrize(
    "override", [dict(model_version=99), dict(url="https://a.example/outra"), dict(model="rss")]
)
def test_a_test_from_another_configuration_is_rejected(repo, override):
    site = new_site(repo)
    link = new_link(repo, site, "https://a.example/feed")
    with pytest.raises(CatalogError):
        repo.record_test(link["id"], link["version"], proof(repo, link["id"], **override))
    assert repo.endpoint(link["id"])["evidence"] == {}


def test_endpoint_recent_proof_flags_a_changed_mechanism_version(repo):
    site = new_site(repo)
    link = new_link(repo, site, "https://a.example/feed")
    repo.record_test(link["id"], link["version"], proof(repo, link["id"]))
    assert repo.endpoint(link["id"])["test_config_current"]
    model = repo.model_settings("auto")
    repo.save_model("auto", dict(version=model["version"], parameters=dict(max_requests=10)))
    assert not repo.endpoint(link["id"])["test_config_current"]


def test_models_are_seeded_and_configuration_is_versioned(repo):
    models = repo.models()
    assert {m["id"] for m in models} >= {"auto", "rss", "sitemap", "sitemap_tree"}
    settings = repo.model_settings("rss")
    assert settings["version"] == 1
    saved = repo.save_model("rss", dict(version=1, parameters=dict(max_requests=12)))
    assert saved["version"] == 2 and saved["parameters"]["max_requests"] == 12
    with pytest.raises(CatalogError) as stale:
        repo.save_model("rss", dict(version=1, parameters=dict(max_requests=13)))
    assert stale.value.status == 409
    unchanged = repo.save_model("rss", dict(version=2, parameters=dict(max_requests=12)))
    assert unchanged["version"] == 2


def test_install_is_idempotent_and_keeps_customised_mechanisms(repo):
    repo.save_model("rss", dict(version=1, parameters=dict(max_requests=12)))
    repo.install()
    assert repo.model_settings("rss")["parameters"]["max_requests"] == 12


def provenance(repo: CatalogRepository, table: str, ident: int) -> dict[str, Any]:
    with repo.connection() as db:
        return dict(
            db.execute(
                f"SELECT deactivated_by,deactivated_reason,deactivated_at IS NOT NULL AS has_at "
                f"FROM {table} WHERE id=%s",
                (ident,),
            ).fetchone()
        )


@pytest.mark.parametrize("entity", ["endpoint", "source"])
def test_a_manual_inactivation_records_the_user_and_a_reactivation_clears_it(repo, entity):
    site = new_site(repo)
    saved = new_link(repo, site, "https://a.example/feed") if entity == "endpoint" else site
    table = "bm_links" if entity == "endpoint" else "bm_sites"
    off = repo.save(entity, dict(version=saved["version"], active=False), saved["id"])
    assert provenance(repo, table, saved["id"]) == dict(
        deactivated_by="user", deactivated_reason="manual", has_at=True
    )
    repo.save(entity, dict(version=off["version"], active=True), saved["id"])
    assert provenance(repo, table, saved["id"]) == dict(
        deactivated_by=None, deactivated_reason=None, has_at=False
    )


def test_a_link_created_inactive_records_the_user(repo):
    link = new_link(repo, new_site(repo), "https://a.example/off", active=False)
    assert provenance(repo, "bm_links", link["id"])["deactivated_by"] == "user"


def test_an_edit_of_an_inactive_link_keeps_the_original_provenance(repo):
    link = new_link(repo, new_site(repo), "https://a.example/off", active=False)
    with repo.connection() as db:
        db.execute("UPDATE bm_links SET deactivated_by='health' WHERE id=%s", (link["id"],))
    repo.save("endpoint", dict(version=link["version"], label="novo"), link["id"])
    assert provenance(repo, "bm_links", link["id"])["deactivated_by"] == "health"
