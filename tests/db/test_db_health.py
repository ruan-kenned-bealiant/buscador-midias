"""Rodadas de saúde sobre ``bm_runs``: criação, elegibilidade, histórico e cancelamento."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest
from health_support import build, set_state
from psycopg.types.json import Jsonb

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.health import HealthRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("BM_TEST_POSTGRES") != "1", reason="Requer PostgreSQL de teste explícito."
)

FAILED = dict(
    url="https://jose.example/feed/",
    source_url="https://jose.example",
    model="rss",
    model_version=1,
    endpoint_accessible=True,
    samples=[dict(accessed=False, status=403)],
)


@pytest.fixture
def world(schema_name: str) -> dict[str, Any]:
    health, catalog, rows = build(schema_name)
    return dict(health=health, catalog=catalog, **rows)


def rows_of(health: HealthRepository, sql: str, *args: Any) -> list[dict[str, Any]]:
    with health.connection() as db:
        return db.execute(sql, args).fetchall()


def deactivate(world: dict[str, Any], ident: int | None = None, **columns: Any) -> None:
    """Inativa um link direto no banco (como uma decisão anterior), com a autoria dada."""
    ident = ident or world["link"]["id"]
    sets = ",".join(f"{c}=%s" for c in ("active", *columns))
    with world["health"].connection() as db:
        db.execute(
            f"UPDATE bm_links SET {sets},version=version+1 WHERE id=%s",
            (0, *columns.values(), ident),
        )


def test_an_active_run_freezes_the_eligible_links_and_the_configuration(world):
    health = world["health"]
    run = health.create("active")
    assert run["total"] == 1 and run["status"] == "queued" and run["kind"] == "active"
    [item] = rows_of(health, "SELECT * FROM bm_run_links WHERE run_id=%s", run["id"])
    assert (item["state"], item["host"], item["link_id"]) == ("pending", "jose.example", 1)
    assert item["link_version"] == world["link"]["version"]
    assert item["site_version"] == world["site"]["version"] and item["mechanism_version"] == 1
    frozen = run["config"]["mechanisms"]["rss"]
    assert frozen["version"] == 1 and frozen["parameters"]["lookback_days"] == 0
    assert frozen["parameters"]["article_samples"] >= 1
    assert run["config"]["max_attempts"] == 2


def test_two_open_runs_of_the_same_kind_are_refused(world):
    world["health"].create("active")
    with pytest.raises(CatalogError) as refused:
        world["health"].create("active")
    assert refused.value.status == 409


def test_the_catalog_is_untouched_by_creating_a_run(world):
    before = world["catalog"].endpoint(world["link"]["id"])
    world["health"].create("active")
    assert world["catalog"].endpoint(world["link"]["id"]) == before


def test_unknown_kind_and_bad_options_are_rejected(world):
    health = world["health"]
    with pytest.raises(CatalogError):
        health.create("nothing")
    with pytest.raises(CatalogError):
        health.create("recovery", options=dict(bright_data="yes"))
    with pytest.raises(CatalogError):
        health.create("recovery", options=dict(scope="everything"))
    with pytest.raises(CatalogError):
        health.create("recovery", "scheduled", options=dict(scope="inactive"))
    with pytest.raises(CatalogError):
        health.create("active", options=dict(scope="inactive"))


def test_recovery_takes_access_failures_only(world):
    health, catalog, site = world["health"], world["catalog"], world["site"]
    set_state(health, 1, last_test=FAILED)
    deactivate(world, deactivated_by="validation")
    manual = catalog.save(
        "endpoint",
        dict(url=f"{site['url']}/manual", kind="feed", active=False),
        source_id=site["id"],
    )
    audio = catalog.save(
        "endpoint",
        dict(url=f"{site['url']}/audio", kind="feed", active=False),
        source_id=site["id"],
    )
    set_state(health, audio["id"], last_test=dict(samples=[dict(accessed=True)]))
    gone = catalog.save(
        "endpoint", dict(url=f"{site['url']}/gone", kind="feed", active=False), source_id=site["id"]
    )
    catalog.delete("endpoint", gone["id"], gone["version"])
    run = health.create("recovery")
    assert run["total"] == 1
    [item] = rows_of(health, "SELECT link_id FROM bm_run_links WHERE run_id=%s", run["id"])
    assert item["link_id"] == 1 and manual["id"] != 1 and audio["id"] != 1


def proved_config(world: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """O que a saúde testou no link 1, igual ao cadastro (ou com a diferença pedida)."""
    return dict(
        dict(
            url="https://jose.example/feed/",
            kind="feed",
            model="rss",
            model_version=1,
            source_url="https://jose.example",
            access_transport="direct",
        ),
        **changes,
    )


def age(world: dict[str, Any], inactivated_days_ago: int | None, checked_days_ago: int) -> None:
    """Link 1 inativo com falha de acesso; datas da inativação pela saúde e da última checagem."""
    health = world["health"]
    set_state(health, 1, last_test=FAILED, health_category="access_error")
    with health.connection() as db:
        db.execute(
            "UPDATE bm_link_state SET health_checked_at=now()-make_interval(days=>%s),"
            "health_config=%s WHERE link_id=1",
            (checked_days_ago, Jsonb(proved_config(world))),
        )
        if inactivated_days_ago is None:
            db.execute("UPDATE bm_links SET active=0,version=version+1 WHERE id=1")
        else:
            db.execute(
                "UPDATE bm_links SET active=0,version=version+1,deactivated_by='health',"
                "deactivated_at=now()-make_interval(days=>%s) WHERE id=1",
                (inactivated_days_ago,),
            )
        db.execute("UPDATE bm_settings SET next_run_at=now()-interval '1 minute'")


@pytest.mark.parametrize(
    ("inactivated_days_ago", "checked_days_ago", "included"),
    [(1, 1, 1), (30, 1, 0), (None, 1, 0), (None, 8, 1)],
)
def test_scheduled_recovery_skips_old_inactives_checked_this_week(
    world, inactivated_days_ago, checked_days_ago, included
):
    age(world, inactivated_days_ago, checked_days_ago)
    assert world["health"].create("recovery", "scheduled")["total"] == included


@pytest.mark.parametrize("scope", ["errors", "inactive"])
def test_the_weekly_rule_applies_to_both_scopes_of_the_scheduled_routine(world, scope):
    age(world, None, 1)
    with world["health"].connection() as db:
        db.execute(
            "UPDATE bm_settings SET value=jsonb_set(value,'{scope}',to_jsonb(%s::text)) "
            "WHERE scope='health_recovery'",
            (scope,),
        )
    assert world["health"].create("recovery", "scheduled")["total"] == 0


def test_a_manual_run_always_tests_everything_in_its_scope(world):
    age(world, None, 1)
    assert world["health"].create("recovery")["total"] == 1


def test_a_manual_test_newer_than_the_health_check_wins(world):
    """A saúde achou o link saudável; o teste manual, depois, diz acesso: vale o manual."""
    health = world["health"]
    set_state(
        health,
        1,
        last_test=dict(FAILED, observed_at=datetime.now(UTC).isoformat()),
        health_category="healthy",
        health_checked_at=datetime(2026, 1, 1, tzinfo=UTC),
        health_config=proved_config(world),
    )
    deactivate(world)
    assert health.create("recovery")["total"] == 1


def test_a_health_check_of_another_configuration_proves_nothing(world):
    """A saúde viu acesso negado, mas em outra URL: o resultado não descreve o link de hoje."""
    health = world["health"]
    set_state(
        health,
        1,
        health_category="access_error",
        health_checked_at=datetime.now(UTC),
        health_config=proved_config(world, url="https://jose.example/old/"),
    )
    deactivate(world)
    assert health.create("recovery")["total"] == 0


def test_toggling_a_link_does_not_invalidate_what_the_health_check_proved(world):
    health = world["health"]
    set_state(
        health,
        1,
        health_category="access_error",
        health_checked_at=datetime.now(UTC),
        health_config=proved_config(world),
    )
    deactivate(world)
    assert health.create("recovery")["total"] == 1


def test_scheduled_runs_wait_for_the_time_and_do_not_overlap(world):
    health = world["health"]
    assert health.create("active", "scheduled") is None  # a primeira execução fica para depois
    with health.connection() as db:
        db.execute("UPDATE bm_settings SET next_run_at=now()-interval '1 minute'")
    run = health.create("active", "scheduled")
    assert run and run["trigger"] == "scheduled"
    assert health.create("active", "scheduled") is None
    [setting] = [s for s in health.settings() if s["kind"] == "active"]
    assert setting["next_run_at"] > datetime.now(UTC)


def test_a_disabled_routine_never_runs_on_schedule(world):
    health = world["health"]
    with health.connection() as db:
        db.execute(
            "UPDATE bm_settings SET value=jsonb_set(value,'{enabled}','false'),"
            "next_run_at=now()-interval '1 minute' WHERE scope='health_active'"
        )
    assert health.create("active", "scheduled") is None


def test_settings_are_listed_by_routine_and_can_be_changed(world):
    health = world["health"]
    assert {s["kind"] for s in health.settings()} == {"active", "recovery"}
    [active] = [s for s in health.settings() if s["kind"] == "active"]
    updated = health.configure("active", dict(version=active["version"], interval_minutes=30000))
    assert updated["interval_minutes"] == 30000 and updated["version"] == active["version"] + 1
    with pytest.raises(CatalogError) as stale:
        health.configure("active", dict(version=active["version"], interval_minutes=60))
    assert stale.value.status == 409


def test_the_overview_lists_runs_and_counts_results(world):
    health = world["health"]
    run = health.create("active")
    with health.connection() as db:
        db.execute("UPDATE bm_run_links SET state='completed',category='healthy'")
    overview = health.overview()
    assert overview["total"] == 1 and overview["runs"][0]["id"] == run["id"]
    assert overview["runs"][0]["counts"] == {"healthy": 1}
    assert overview["runs"][0]["configuration"]["workers"] >= 1


def test_the_details_page_filters_by_category_and_exposes_the_attempt_log(world):
    health = world["health"]
    run = health.create("active")
    with health.connection() as db:
        db.execute(
            "UPDATE bm_run_links SET state='completed',category='access_error',attempts=2,"
            'result=\'{"attempt_log":[{"attempt":1},{"attempt":2}]}\''
        )
    detail = health.details(run["id"], category="access_error")
    [item] = detail["items"]
    assert item["url"].endswith("/feed/") and item["source_name"] == "José"
    assert len(item["history"]) == 2 and detail["total"] == 1
    assert "result" not in item["history"][0] and item["history"][1]["result"] == {}
    assert item["snapshot"]["config"]["url"].endswith("/feed/")
    assert item["snapshot"]["source"]["name"] == "José"
    assert health.details(run["id"], category="healthy")["items"] == []
    with pytest.raises(CatalogError) as missing:
        health.details("nope")
    assert missing.value.status == 404


def test_cancel_only_applies_to_an_open_run(world):
    health = world["health"]
    run = health.create("active")
    assert health.cancel(run["id"])["status"] == "cancelling"
    with health.connection() as db:
        db.execute("UPDATE bm_runs SET status='cancelled'")
    with pytest.raises(CatalogError) as closed:
        health.cancel(run["id"])
    assert closed.value.status == 409


# --- varredura de links excluídos pela validação ----------------------------------------------


def validation_excluded(world: dict[str, Any], ident: int = 1) -> None:
    set_state(
        world["health"], ident, last_test=dict(endpoint_accessible=False, warnings=["HTTP 403"])
    )
    with world["health"].connection() as db:
        db.execute(
            "UPDATE bm_links SET active=0,deleted_at=now(),deactivated_by='validation',"
            "deactivated_at=now(),version=version+1 WHERE id=%s",
            (ident,),
        )


RESTORE = dict(scope="auto_excluded_access", activate_recovered=True)


def test_only_automatic_access_exclusions_enter_the_restoration(world):
    other = world["catalog"].save(
        "endpoint",
        dict(url="https://jose.example/other", kind="feed"),
        source_id=world["site"]["id"],
    )
    validation_excluded(world)
    validation_excluded(world, other["id"])
    with world["health"].connection() as db:  # o site também foi inativado pela validação
        db.execute(
            "UPDATE midias_ia_trusted_sources SET active=false,deactivated_by='validation',version=version+1"
        )
    run = world["health"].create("recovery", options=RESTORE)
    assert run["total"] == 2 and run["config"]["scope"] == "auto_excluded_access"


@pytest.mark.parametrize("manual", ["link", "source", "audio"])
def test_manual_decisions_and_non_access_failures_stay_out_of_the_restoration(world, manual):
    validation_excluded(world)
    with world["health"].connection() as db:
        if manual == "link":
            db.execute("UPDATE bm_links SET deactivated_by='user' WHERE id=1")
        elif manual == "source":
            db.execute("UPDATE midias_ia_trusted_sources SET active=false,deactivated_by='user'")
        else:
            set_state(world["health"], 1, last_test=dict(samples=[dict(accessed=True)]))
    assert world["health"].create("recovery", options=RESTORE)["total"] == 0


def test_a_run_of_an_unknown_scheduler_is_not_created_without_bright_credentials(
    world, monkeypatch
):
    monkeypatch.setattr("buscador_midias.db.health.bright_data_available", lambda: False)
    with world["health"].connection() as db:
        db.execute(
            "UPDATE bm_settings SET value=jsonb_set(value,'{bright_data}','true') "
            "WHERE scope='health_active'"
        )
    with pytest.raises(CatalogError):
        world["health"].create("active")


def test_runs_of_other_types_do_not_block_or_appear(world):
    health = world["health"]
    with health.connection() as db:
        db.execute(
            "INSERT INTO bm_runs(id,type,trigger,status,config) "
            "VALUES('s1','search','manual','running','{}')"
        )
    health.create("active")
    assert [r["id"] for r in health.overview()["runs"]] != ["s1"]
    assert health.overview()["total"] == 1
