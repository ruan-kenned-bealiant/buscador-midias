"""``bm.sh`` entrega ao pipeline o broker do buscador, sem o prefixo das filas do serviço antigo."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bm.sh"
RABBIT_ENV = (
    "RABBITMQ_HOST=broker.local\nRABBITMQ_PORT=5673\nRABBITMQ_VHOST=buscador\n"
    "BM_QUEUE_PREFIX=midias.daily.v1\nBM_INGEST_WORK_DIR=/dados/trabalho\n"
)
FAKE_PYTHON = """\
#!/usr/bin/env bash
for name in RABBITMQ_HOST RABBITMQ_PORT RABBITMQ_VHOST BM_QUEUE_PREFIX BM_INGEST_WORK_DIR; do
  echo "$name=${!name-<ausente>}"
done
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "bm.sh")
    (tmp_path / "var" / "ingestion").mkdir(parents=True)
    (tmp_path / "var" / "ingestion" / "rabbit.env").write_text(RABBIT_ENV, encoding="utf-8")
    (tmp_path / "kyx.env").write_text("OPENROUTER_API_KEY=x\n", encoding="utf-8")
    fake = tmp_path / "python"
    fake.write_text(FAKE_PYTHON, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def run(project: Path, command: str) -> dict[str, str]:
    import os

    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("RABBITMQ_", "BM_"))
    }
    environment.update(PY=str(project / "python"), KYX_ENV=str(project / "kyx.env"))
    done = subprocess.run(
        ["bash", str(project / "scripts" / "bm.sh"), command],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return dict(line.split("=", 1) for line in done.stdout.splitlines())


def test_the_pipeline_gets_the_broker_but_not_the_old_queue_prefix(project: Path):
    seen = run(project, "pipeline")
    assert seen["RABBITMQ_HOST"] == "broker.local" and seen["RABBITMQ_VHOST"] == "buscador"
    assert seen["BM_INGEST_WORK_DIR"] == "/dados/trabalho"
    assert seen["BM_QUEUE_PREFIX"] == "<ausente>"


def test_the_panel_reads_the_same_queues_as_the_pipeline(project: Path):
    seen = run(project, "dashboard")
    assert seen["RABBITMQ_HOST"] == "broker.local" and seen["BM_QUEUE_PREFIX"] == "<ausente>"


def test_the_old_ingest_service_keeps_its_own_prefix(project: Path):
    assert run(project, "ingest")["BM_QUEUE_PREFIX"] == "midias.daily.v1"


def test_other_commands_do_not_receive_the_broker(project: Path):
    assert run(project, "db")["RABBITMQ_HOST"] == "<ausente>"
