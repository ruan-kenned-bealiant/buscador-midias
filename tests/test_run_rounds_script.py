import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from buscador_midias.dashboard.schedule import build_schedule, load_schedule

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_rounds.sh"
START = int(datetime(2026, 10, 15, 12, 0, tzinfo=UTC).timestamp())


def run(*args: str, tmp_path: Path, env: dict[str, str] | None = None):
    environment = {
        **os.environ,
        "TZ": "UTC",
        "START_EPOCH": str(START),
        "SCHEDULE_FILE": str(tmp_path / "agenda.json"),
        **(env or {}),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def test_dry_run_mostra_o_plano_sem_executar_nada(tmp_path: Path):
    result = run(
        "--dry-run", "--count", "3", "--first-delay", "600", "--interval", "7200", tmp_path=tmp_path
    )

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert "rodada 1/3 prevista para 2026-10-15 12:10:00" in lines
    assert "rodada 2/3 prevista para 2026-10-15 14:10:00" in lines
    assert "rodada 3/3 prevista para 2026-10-15 16:10:00" in lines
    assert not (tmp_path / "agenda.json").exists()


def test_write_schedule_grava_um_json_que_o_painel_entende(tmp_path: Path):
    result = run(
        "--dry-run",
        "--write-schedule",
        "--triage-at-end",
        "--count",
        "2",
        "--first-delay",
        "3600",
        "--interval",
        "7200",
        tmp_path=tmp_path,
    )

    assert result.returncode == 0
    payload = json.loads((tmp_path / "agenda.json").read_text(encoding="utf-8"))
    assert [(j["kind"], j["at"]) for j in payload["jobs"]] == [
        ("discovery", "2026-10-15T13:00:00+00:00"),
        ("discovery", "2026-10-15T15:00:00+00:00"),
        ("triage", "2026-10-15T15:03:00+00:00"),
    ]
    jobs = load_schedule(tmp_path / "agenda.json")
    assert [j.name for j in jobs][:1] == ["Rodada agendada 1/2"]
    schedule = build_schedule(
        path=tmp_path / "agenda.json",
        discovery_runs=[],
        triage_runs=[],
        now=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
    )
    assert [j["state"] for j in schedule["jobs"]] == ["upcoming"] * 3


def test_agenda_sem_triagem_termina_com_uma_rodada_valida(tmp_path: Path):
    run("--dry-run", "--write-schedule", "--count", "1", tmp_path=tmp_path)

    payload = json.loads((tmp_path / "agenda.json").read_text(encoding="utf-8"))

    assert len(payload["jobs"]) == 1  # sem vírgula sobrando: o JSON é válido


def test_opcao_desconhecida_falha_com_mensagem(tmp_path: Path):
    result = run("--nao-existe", tmp_path=tmp_path)

    assert result.returncode == 2
    assert "opção desconhecida" in result.stderr


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_ajuda_lista_as_opcoes(flag: str, tmp_path: Path):
    result = run(flag, tmp_path=tmp_path)

    assert result.returncode == 0
    assert "--triage-at-end" in result.stdout
    assert "--dry-run" in result.stdout


def test_triagem_a_cada_rodada_aparece_no_plano_e_na_agenda(tmp_path: Path):
    result = run(
        "--dry-run",
        "--write-schedule",
        "--triage-each-round",
        "--count",
        "2",
        "--first-delay",
        "3600",
        "--interval",
        "7200",
        tmp_path=tmp_path,
    )

    assert result.returncode == 0
    assert "triagem após a rodada 1/2 prevista para 2026-10-15 13:02:00" in result.stdout
    assert "triagem após a rodada 2/2 prevista para 2026-10-15 15:02:00" in result.stdout
    payload = json.loads((tmp_path / "agenda.json").read_text(encoding="utf-8"))
    assert [(j["kind"], j["at"]) for j in payload["jobs"]] == [
        ("discovery", "2026-10-15T13:00:00+00:00"),
        ("triage", "2026-10-15T13:02:00+00:00"),
        ("discovery", "2026-10-15T15:00:00+00:00"),
        ("triage", "2026-10-15T15:02:00+00:00"),
    ]


def test_triagem_por_rodada_e_no_fim_somam_uma_ultima_triagem_de_resgate(tmp_path: Path):
    run(
        "--dry-run",
        "--write-schedule",
        "--triage-each-round",
        "--triage-at-end",
        "--count",
        "1",
        "--first-delay",
        "3600",
        tmp_path=tmp_path,
    )

    payload = json.loads((tmp_path / "agenda.json").read_text(encoding="utf-8"))

    assert [j["kind"] for j in payload["jobs"]] == ["discovery", "triage", "triage"]
    assert payload["jobs"][2]["at"] == "2026-10-15T13:03:00+00:00"


def test_agenda_com_triagem_por_rodada_e_json_valido_e_ordenado(tmp_path: Path):
    run("--dry-run", "--write-schedule", "--triage-each-round", "--count", "3", tmp_path=tmp_path)

    jobs = load_schedule(tmp_path / "agenda.json")

    assert len(jobs) == 6
    assert [j.at for j in jobs] == sorted(j.at for j in jobs)


def test_enriquecimento_a_cada_rodada_aparece_no_plano_e_na_agenda_depois_da_triagem(
    tmp_path: Path,
):
    result = run(
        "--dry-run",
        "--write-schedule",
        "--triage-each-round",
        "--enrich-each-round",
        "--count",
        "2",
        "--first-delay",
        "3600",
        "--interval",
        "7200",
        tmp_path=tmp_path,
    )

    assert result.returncode == 0
    assert "enriquecimento após a rodada 1/2 prevista para 2026-10-15 13:05:00" in result.stdout
    payload = json.loads((tmp_path / "agenda.json").read_text(encoding="utf-8"))
    assert [(j["kind"], j["at"]) for j in payload["jobs"]] == [
        ("discovery", "2026-10-15T13:00:00+00:00"),
        ("triage", "2026-10-15T13:02:00+00:00"),
        ("enrich", "2026-10-15T13:05:00+00:00"),
        ("discovery", "2026-10-15T15:00:00+00:00"),
        ("triage", "2026-10-15T15:02:00+00:00"),
        ("enrich", "2026-10-15T15:05:00+00:00"),
    ]
    assert len(load_schedule(tmp_path / "agenda.json")) == 6


def test_enriquecimento_sozinho_sem_triagem_tambem_gera_agenda_valida(tmp_path: Path):
    run("--dry-run", "--write-schedule", "--enrich-each-round", "--count", "2", tmp_path=tmp_path)

    jobs = load_schedule(tmp_path / "agenda.json")

    assert [j.kind for j in jobs] == ["discovery", "enrich", "discovery", "enrich"]


def test_ajuda_lista_a_opcao_de_enriquecimento(tmp_path: Path):
    assert "--enrich-each-round" in run("--help", tmp_path=tmp_path).stdout
