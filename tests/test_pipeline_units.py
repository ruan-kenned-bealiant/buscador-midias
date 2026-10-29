"""Os units do systemd do pipeline só usam papéis e etapas que o serviço aceita."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from buscador_midias.db.retention import CATEGORIES
from buscador_midias.pipeline import broker
from buscador_midias.pipeline.service import build_parser

UNITS = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
PIPELINE = sorted(
    p
    for p in UNITS.glob("buscador-midias-*.service")
    if not any(word in p.name for word in ("ingest", "painel", "limpeza"))
)


def command(unit: Path) -> list[str]:
    exec_start = re.search(r"^ExecStart=(.+)$", unit.read_text(), re.M)
    assert exec_start, unit.name
    words = shlex.split(exec_start.group(1))
    assert words[0].endswith("scripts/bm.sh") and words[1] == "pipeline"
    return words[2:]


def test_there_is_one_unit_per_role():
    assert {p.name for p in PIPELINE} == {
        "buscador-midias-scheduler.service",
        "buscador-midias-publisher.service",
        "buscador-midias-window.service",
        "buscador-midias-health.service",
        "buscador-midias-worker@.service",
    }


@pytest.mark.parametrize("unit", PIPELINE, ids=lambda p: p.name)
def test_each_unit_command_is_accepted_by_the_service_parser(unit: Path):
    words = [w.replace("%i", broker.STAGES[0]) for w in command(unit)]
    args = build_parser().parse_args(words)
    assert args.command == "serve"


@pytest.mark.parametrize("unit", PIPELINE, ids=lambda p: p.name)
def test_units_never_read_the_old_service_environment(unit: Path):
    """O arquivo do serviço antigo traz o prefixo das filas dele; ``bm.sh`` escolhe o certo."""
    assert "EnvironmentFile" not in unit.read_text()


def test_the_panel_unit_runs_on_the_new_tables():
    text = (UNITS / "buscador-midias-painel.service").read_text()
    assert "Environment=BM_BACKEND=bm" in text and "scripts/bm.sh dashboard" in text


def test_the_worker_template_takes_the_stage_from_the_instance_name():
    assert "--stage %i" in (UNITS / "buscador-midias-worker@.service").read_text()


@pytest.mark.parametrize("unit", PIPELINE, ids=lambda p: p.name)
def test_units_wait_long_enough_for_a_clean_stop(unit: Path):
    assert "TimeoutStopSec=150" in unit.read_text()


CLEANING = UNITS / "buscador-midias-limpeza@.service"
TIMERS = {c: UNITS / f"buscador-midias-limpeza-{c}.timer" for c in CATEGORIES}


def test_each_cleaning_category_has_its_own_timer():
    assert all(t.exists() for t in TIMERS.values())
    assert {p.name for p in UNITS.glob("buscador-midias-limpeza-*.timer")} == {
        t.name for t in TIMERS.values()
    }


def test_the_cleaning_service_runs_one_category_and_exits():
    text = CLEANING.read_text()
    assert "Type=oneshot" in text and "EnvironmentFile" not in text
    exec_start = re.search(r"^ExecStart=(.+)$", text, re.M)
    assert exec_start
    words = shlex.split(exec_start.group(1))
    assert words[0].endswith("scripts/bm.sh") and words[1:4] == ["db", "clean", "%i"]


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_a_timer_fires_its_own_category_every_day_and_catches_up(category: str):
    text = TIMERS[category].read_text()
    assert f"Unit=buscador-midias-limpeza@{category}.service" in text
    assert re.search(r"^OnCalendar=\*-\*-\* \d\d:\d\d:00$", text, re.M)
    assert "Persistent=true" in text and "WantedBy=timers.target" in text


def test_each_timer_fires_at_the_time_the_panel_announces():
    """O horário mostrado na tela vem de ``Category.at``; o timer precisa marcar o mesmo."""
    for category, timer in TIMERS.items():
        match = re.search(r"^OnCalendar=\*-\*-\* (\d\d:\d\d):00$", timer.read_text(), re.M)
        assert match and match[1] == CATEGORIES[category].at, category


def test_the_cleanings_never_start_together_nor_during_a_search_round():
    times = [c.at for c in CATEGORIES.values()]
    assert len(set(times)) == len(times)
    # As rodadas de busca saem às 01, 06, 12 e 16h e a de 01h dura cerca de uma hora.
    assert all(at.split(":")[0] in ("03", "04") for at in times)
