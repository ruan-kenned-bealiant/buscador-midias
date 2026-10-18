import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from buscador_midias.dashboard.schedule import build_schedule, load_schedule
from buscador_midias.state.store import RunRecord

NOW = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)


def run(run_id: int, started: datetime) -> RunRecord:
    return RunRecord(run_id, started, started + timedelta(minutes=2))


def write(path: Path, *jobs: dict) -> Path:
    path.write_text(json.dumps({"jobs": list(jobs)}), encoding="utf-8")
    return path


def job(name: str, at: datetime, kind: str = "discovery", note: str = "") -> dict:
    return {"name": name, "kind": kind, "at": at.isoformat(), "note": note}


def test_arquivo_ausente_ou_invalido_nao_derruba_nada(tmp_path: Path):
    assert load_schedule(tmp_path / "nao-existe.json") == []
    broken = tmp_path / "quebrado.json"
    broken.write_text("{ isto nao e json", encoding="utf-8")
    assert load_schedule(broken) == []
    wrong = tmp_path / "errado.json"
    wrong.write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "x"},
                    "texto",
                    {"name": "ok", "kind": "enrich", "at": NOW.isoformat()},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert [j.name for j in load_schedule(wrong)] == ["ok"]


def test_none_e_sem_agenda():
    assert load_schedule(None) == []


def test_agenda_do_arquivo_ordena_e_calcula_a_contagem_regressiva(tmp_path: Path):
    path = write(
        tmp_path / "s.json",
        job("Rodada 3", NOW + timedelta(hours=4)),
        job("Rodada 2", NOW + timedelta(hours=2), note="aprox."),
    )

    schedule = build_schedule(path=path, discovery_runs=[], now=NOW)

    assert schedule["source"] == "arquivo"
    names = [(j["name"], j["state"], j["seconds_until"]) for j in schedule["jobs"]]
    assert names == [("Rodada 2", "upcoming", 7200), ("Rodada 3", "upcoming", 14400)]
    assert schedule["jobs"][0]["note"] == "aprox."


def test_tarefa_ja_executada_aparece_como_concluida(tmp_path: Path):
    at = NOW - timedelta(hours=1)
    path = write(
        tmp_path / "s.json", job("Rodada 1", at), job("Rodada 2", NOW + timedelta(hours=1))
    )
    runs = [run(1, at + timedelta(seconds=40))]

    jobs = build_schedule(path=path, discovery_runs=runs, now=NOW)["jobs"]

    assert [(j["name"], j["state"]) for j in jobs] == [
        ("Rodada 1", "done"),
        ("Rodada 2", "upcoming"),
    ]


def test_tarefa_vencida_sem_execucao_fica_atrasada_e_dentro_da_tolerancia_fica_no_horario(
    tmp_path: Path,
):
    path = write(
        tmp_path / "s.json",
        job("Velha", NOW - timedelta(hours=1)),
        job("Agora", NOW - timedelta(minutes=3)),
    )

    jobs = build_schedule(path=path, discovery_runs=[], now=NOW)["jobs"]

    assert [(j["name"], j["state"]) for j in jobs] == [("Velha", "overdue"), ("Agora", "due")]


def test_so_as_ultimas_concluidas_ficam_visiveis(tmp_path: Path):
    jobs = [job(f"R{n}", NOW - timedelta(hours=10 - n)) for n in range(1, 7)]
    runs = [run(n, NOW - timedelta(hours=10 - n)) for n in range(1, 7)]
    path = write(tmp_path / "s.json", *jobs)

    result = build_schedule(path=path, discovery_runs=runs, now=NOW)["jobs"]

    assert [j["name"] for j in result] == ["R4", "R5", "R6"]


def test_sem_arquivo_estima_pela_cadencia_observada():
    runs = [run(n, NOW - timedelta(hours=2 * (4 - n)) - timedelta(minutes=30)) for n in range(1, 5)]

    schedule = build_schedule(path=None, discovery_runs=runs, now=NOW)

    assert schedule["source"] == "estimado"
    [estimated] = schedule["jobs"]
    assert estimated["kind"] == "discovery"
    assert estimated["state"] == "upcoming"
    assert estimated["seconds_until"] == 5400  # última às 17:30 + 2 h = 19:30


def test_cadencia_curta_demais_nao_gera_estimativa():
    runs = [run(n, NOW - timedelta(minutes=2 * n)) for n in range(1, 5)]

    schedule = build_schedule(path=None, discovery_runs=runs, now=NOW)

    assert schedule["source"] is None
    assert schedule["jobs"] == []


def test_com_menos_de_tres_rodadas_nao_estima():
    runs = [run(1, NOW - timedelta(hours=4)), run(2, NOW - timedelta(hours=2))]

    assert build_schedule(path=None, discovery_runs=runs, now=NOW)["jobs"] == []


def test_tarefa_de_enriquecimento_e_reconhecida_e_concluida_pela_execucao_do_tipo(tmp_path: Path):
    from buscador_midias.state.store import EnrichRun, EnrichTotals

    at = NOW - timedelta(hours=1)
    path = write(tmp_path / "s.json", job("Enriquecimento", at, kind="enrich"))
    enrich_run = EnrichRun(
        1, at, "m", "v", "auto", EnrichTotals(), finished_at=at + timedelta(minutes=2)
    )

    waiting = build_schedule(path=path, discovery_runs=[run(1, at)], now=NOW)
    done = build_schedule(path=path, discovery_runs=[], enrich_runs=[enrich_run], now=NOW)

    assert waiting["jobs"][0]["state"] == "overdue"
    assert done["jobs"][0]["state"] == "done"


def test_tarefa_de_corpus_e_reconhecida_e_concluida_pela_execucao_do_tipo(tmp_path: Path):
    from buscador_midias.state.store import CorpusRun, CorpusTotals

    at = NOW - timedelta(hours=1)
    path = write(tmp_path / "s.json", job("Gravação no corpus", at, kind="corpus"))
    corpus_run = CorpusRun(1, at, CorpusTotals(), finished_at=at + timedelta(minutes=1))

    waiting = build_schedule(path=path, discovery_runs=[run(1, at)], now=NOW)
    done = build_schedule(path=path, discovery_runs=[], corpus_runs=[corpus_run], now=NOW)

    assert waiting["jobs"][0]["state"] == "overdue"
    assert done["jobs"][0]["state"] == "done"


BRT = timezone(timedelta(hours=-3))
DAILY = ["06:00", "12:00", "16:00"]


def write_daily(path: Path, times: list[str]) -> Path:
    path.write_text(json.dumps({"daily": times}), encoding="utf-8")
    return path


def test_horarios_diarios_viram_tarefas_de_ontem_hoje_e_amanha_no_fuso_local(tmp_path: Path):
    now = datetime(2026, 10, 15, 15, 0, tzinfo=BRT)  # 15:00 em Brasília
    path = write_daily(tmp_path / "s.json", DAILY)

    jobs = load_schedule(path, now=now, tz=BRT)

    assert len(jobs) == 9
    assert {job.kind for job in jobs} == {"discovery"}
    assert [job.at.isoformat() for job in jobs[:3]] == [
        "2026-10-14T06:00:00-03:00",
        "2026-10-14T12:00:00-03:00",
        "2026-10-14T16:00:00-03:00",
    ]
    assert jobs[-1].at.isoformat() == "2026-10-16T16:00:00-03:00"
    assert jobs[0].name == "Rodada das 06:00"


def test_agenda_diaria_marca_feitas_a_proxima_e_as_atrasadas(tmp_path: Path):
    now = datetime(2026, 10, 15, 15, 0, tzinfo=BRT)
    path = write_daily(tmp_path / "s.json", DAILY)
    runs = [run(1, datetime(2026, 10, 15, 6, 0, 20, tzinfo=BRT))]  # só a das 06:00 de hoje rodou

    jobs = build_schedule(path=path, discovery_runs=runs, now=now, tz=BRT)["jobs"]

    states = {(j["at"][:16], j["state"]) for j in jobs}
    assert ("2026-10-15T06:00", "done") in states
    assert ("2026-10-15T12:00", "overdue") in states  # passou e não rodou
    assert ("2026-10-15T16:00", "upcoming") in states
    assert ("2026-10-16T06:00", "upcoming") in states


def test_horario_diario_invalido_e_ignorado(tmp_path: Path):
    now = datetime(2026, 10, 15, 15, 0, tzinfo=BRT)
    path = write_daily(tmp_path / "s.json", ["06:00", "25:99", "meio-dia", ""])

    jobs = load_schedule(path, now=now, tz=BRT)

    assert {job.name for job in jobs} == {"Rodada das 06:00"}


def test_horario_diario_anterior_a_primeira_rodada_do_estado_nao_vira_atrasado(tmp_path: Path):
    now = datetime(2026, 10, 15, 15, 0, tzinfo=BRT)
    path = write_daily(tmp_path / "s.json", DAILY)
    first = datetime(2026, 10, 15, 11, 30, tzinfo=BRT)  # o estado só começou às 11:30 de hoje

    jobs = build_schedule(path=path, discovery_runs=[run(1, first)], now=now, tz=BRT)["jobs"]

    assert [j["at"][:16] for j in jobs] == [
        "2026-10-15T12:00",
        "2026-10-15T16:00",
        "2026-10-16T06:00",
        "2026-10-16T12:00",
        "2026-10-16T16:00",
    ]
    assert jobs[0]["state"] == "overdue"  # 12:00 de hoje já passou sem rodada


def test_agenda_desativada_nao_estima_execucao_pelo_historico(tmp_path: Path):
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps({"enabled": False, "daily": ["06:00"]}))
    runs = [run(n, NOW - timedelta(hours=6 - n * 2)) for n in range(3)]
    assert load_schedule(path, now=NOW) == []
    assert build_schedule(path=path, discovery_runs=runs, now=NOW) == {
        "source": "arquivo",
        "jobs": [],
    }
