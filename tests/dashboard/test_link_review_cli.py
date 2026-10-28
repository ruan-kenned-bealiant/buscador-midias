# ruff: noqa: F811 -- fixtures de test_link_review são reutilizadas por nome.
"""Subcomando review-links: probe grava o relatório e apply só mexe no que foi escolhido."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime

import pytest
from test_link_review import NOW, Fleet, add_link, store  # noqa: F401

from buscador_midias.catalog.review_cli import main
from buscador_midias.catalog.store import CatalogError


def run(store, tmp_path, *argv, ages=None):
    return main(
        [*argv],
        store=store,
        make_reader=Fleet(ages or {}),
        now=NOW,
        root=tmp_path / "revisao",
    )


def reports(tmp_path):
    return sorted((tmp_path / "revisao").glob("*/report.json"))


@pytest.fixture
def links(store):
    return dict(
        novo=add_link(store, 1, "novo"),
        parado=add_link(store, 1, "parado"),
        morto=add_link(store, 2, "morto"),
        receitas=add_link(store, 2, "receitas"),
    )


AGES = {"novo": 1, "parado": 90, "morto": 500}


def test_probe_writes_json_and_csv_and_changes_nothing(store, tmp_path, links):
    assert run(store, tmp_path, "probe", "--site-id", "1", "2", ages=AGES) == 0
    (report,) = reports(tmp_path)
    data = json.loads(report.read_text())
    assert {r["proposal"] for r in data["results"]} == {
        "keep",
        "inactivate",
        "delete",
        "remove_theme",
    }
    assert {s["id"] for s in data["sites"]} == {1, 2}
    rows = list(csv.DictReader((report.parent / "report.csv").open()))
    assert len(rows) == 4 and {"site", "url", "proposal", "reason"} <= set(rows[0])
    assert all(store.endpoint(i["id"])["active"] for i in links.values())


def test_sites_can_be_chosen_by_name(store, tmp_path, links):
    assert run(store, tmp_path, "probe", "--name", "alfa", ages=AGES) == 0
    data = json.loads(reports(tmp_path)[0].read_text())
    assert [s["id"] for s in data["sites"]] == [1]
    assert len(data["results"]) == 2


def test_largest_skips_sites_already_reviewed(store, tmp_path, links):
    run(store, tmp_path, "probe", "--site-id", "1", ages=AGES)
    run(store, tmp_path, "probe", "--largest", "1", ages=AGES)
    second = json.loads(reports(tmp_path)[-1].read_text())
    assert [s["id"] for s in second["sites"]] == [2]


def test_probe_without_a_selection_is_a_usage_error(store, tmp_path):
    with pytest.raises(SystemExit):
        run(store, tmp_path, "probe")


def test_apply_changes_only_the_chosen_proposals(store, tmp_path, links):
    run(store, tmp_path, "probe", "--site-id", "1", "2", ages=AGES)
    (report,) = reports(tmp_path)
    assert run(store, tmp_path, "apply", str(report), "--proposals", "inactivate") == 0
    assert not store.endpoint(links["parado"]["id"])["active"]
    assert store.endpoint(links["novo"]["id"])["active"]
    with store.connection() as db:
        deleted = {r["id"] for r in db.execute("SELECT id FROM links WHERE deleted_at IS NOT NULL")}
    assert deleted == set()


def test_apply_requires_an_explicit_choice_of_proposals(store, tmp_path, links):
    run(store, tmp_path, "probe", "--site-id", "1", ages=AGES)
    (report,) = reports(tmp_path)
    with pytest.raises(SystemExit):
        run(store, tmp_path, "apply", str(report))


def test_apply_never_accepts_review_or_keep(store, tmp_path, links):
    run(store, tmp_path, "probe", "--site-id", "1", ages=AGES)
    (report,) = reports(tmp_path)
    with pytest.raises(SystemExit):
        run(store, tmp_path, "apply", str(report), "--proposals", "review")


def test_the_report_records_when_it_was_applied(store, tmp_path, links):
    run(store, tmp_path, "probe", "--site-id", "1", ages=AGES)
    (report,) = reports(tmp_path)
    run(store, tmp_path, "apply", str(report), "--proposals", "inactivate", "delete")
    assert json.loads(report.read_text())["applied"]["inactivate"] == 1


def test_it_uses_the_run_instant_for_the_report_folder(store, tmp_path, links):
    run(store, tmp_path, "probe", "--site-id", "1", ages=AGES)
    (report,) = reports(tmp_path)
    assert report.parent.name.startswith(NOW.astimezone(UTC).strftime("%Y%m%dT%H%M%S"))
    assert datetime.fromisoformat(json.loads(report.read_text())["probed_at"]) == NOW


def test_historical_lists_only_closed_periods_and_reads_nothing(store, tmp_path):
    add_link(store, 1, "sitemap/2026/09/01.xml")
    add_link(store, 1, "sitemap-2026-10.xml")
    add_link(store, 2, "sitemap/2026/10/19_1.xml")
    keep = add_link(store, 2, "feed.xml")
    fleet = Fleet({})
    assert (
        main(["historical"], store=store, make_reader=fleet, now=NOW, root=tmp_path / "revisao")
        == 0
    )
    (report,) = reports(tmp_path)
    data = json.loads(report.read_text())
    assert {r["proposal"] for r in data["results"]} == {"remove_historical"}
    assert sorted(r["url"].rsplit("/", 1)[1] for r in data["results"]) == ["01.xml", "19_1.xml"]
    assert fleet.asked == []
    assert store.endpoint(keep["id"])["active"]


def test_apply_removes_the_historical_links_and_only_those(store, tmp_path):
    old = add_link(store, 1, "sitemap/2026/09/01.xml")
    live = add_link(store, 1, "sitemap-2026-10.xml")
    main(["historical"], store=store, now=NOW, root=tmp_path / "revisao")
    (report,) = reports(tmp_path)
    assert main(["apply", str(report), "--proposals", "remove_historical"], store=store) == 0
    with pytest.raises(CatalogError, match="excluído"):
        store.endpoint(old["id"])
    assert store.endpoint(live["id"])["active"]
