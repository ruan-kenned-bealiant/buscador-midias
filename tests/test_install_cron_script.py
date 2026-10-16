import json
import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_cron.sh"
ROOT = SCRIPT.parents[1]

FAKE_CRONTAB = """\
#!/usr/bin/env bash
# crontab de mentirinha: guarda em $FAKE_CRONTAB_FILE
if [ "$1" = "-l" ]; then
  [ -f "$FAKE_CRONTAB_FILE" ] || { echo "no crontab" >&2; exit 1; }
  cat "$FAKE_CRONTAB_FILE"
elif [ "$1" = "-" ]; then
  cat > "$FAKE_CRONTAB_FILE"
fi
"""


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.crontab_file = tmp_path / "crontab.txt"
        self.schedule_file = tmp_path / "schedule.json"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "crontab"
        fake.write_text(FAKE_CRONTAB, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.bin_dir = bin_dir

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "FAKE_CRONTAB_FILE": str(self.crontab_file),
            "SCHEDULE_FILE": str(self.schedule_file),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )

    def crontab(self) -> list[str]:
        return self.crontab_file.read_text(encoding="utf-8").splitlines()


def test_instala_uma_linha_por_horario_chamando_o_run_sh(tmp_path: Path):
    env = Env(tmp_path)

    result = env.run("--times", "06:00,12:00,16:00")

    assert result.returncode == 0
    lines = [line for line in env.crontab() if not line.startswith("#")]
    assert len(lines) == 3
    assert lines[0].startswith("0 6 * * * ")
    assert lines[1].startswith("0 12 * * * ")
    assert lines[2].startswith("0 16 * * * ")
    assert all(f"cd {ROOT} && scripts/run.sh" in line for line in lines)
    assert all(line.endswith(">> var/run.log 2>&1") for line in lines)


def test_minutos_vao_para_o_campo_certo(tmp_path: Path):
    env = Env(tmp_path)

    env.run("--times", "06:05,23:30")

    lines = [line for line in env.crontab() if not line.startswith("#")]
    assert [line.split(" ")[:2] for line in lines] == [["5", "6"], ["30", "23"]]


def test_grava_a_agenda_do_painel_com_os_mesmos_horarios(tmp_path: Path):
    env = Env(tmp_path)

    env.run("--times", "06:00,12:00,16:00")

    assert json.loads(env.schedule_file.read_text(encoding="utf-8")) == {
        "daily": ["06:00", "12:00", "16:00"]
    }


def test_repetir_nao_duplica_e_preserva_o_resto_do_crontab(tmp_path: Path):
    env = Env(tmp_path)
    env.crontab_file.write_text("MAILTO=eu@exemplo.com\n30 3 * * * backup-do-banco\n")

    env.run("--times", "06:00")
    env.run("--times", "07:00,13:00")

    content = env.crontab()
    assert "MAILTO=eu@exemplo.com" in content
    assert "30 3 * * * backup-do-banco" in content
    jobs = [line for line in content if "run.sh" in line]
    assert [line.split(" ")[:2] for line in jobs] == [["0", "7"], ["0", "13"]]
    assert content.count("# >>> buscador-midias >>>") == 1


def test_remove_so_o_bloco_do_buscador(tmp_path: Path):
    env = Env(tmp_path)
    env.crontab_file.write_text("30 3 * * * backup-do-banco\n")
    env.run("--times", "06:00")

    result = env.run("--remove")

    assert result.returncode == 0
    assert env.crontab() == ["30 3 * * * backup-do-banco"]


def test_dry_run_mostra_o_resultado_sem_instalar_nada(tmp_path: Path):
    env = Env(tmp_path)

    result = env.run("--dry-run", "--times", "06:00")

    assert result.returncode == 0
    assert "0 6 * * * " in result.stdout
    assert not env.crontab_file.exists()
    assert not env.schedule_file.exists()


def test_horario_invalido_falha_sem_mexer_em_nada(tmp_path: Path):
    env = Env(tmp_path)

    for bad in ("25:00", "6:00", "meio-dia", "06:00,12h"):
        result = env.run("--times", bad)
        assert result.returncode == 2, bad
        assert "horário inválido" in result.stderr

    assert not env.crontab_file.exists()


def test_amostra_de_fontes_vai_para_o_comando(tmp_path: Path):
    env = Env(tmp_path)

    env.run("--times", "06:00", "--sample", "data/outra.txt")

    [job] = [line for line in env.crontab() if "run.sh" in line]
    assert "scripts/run.sh --only-file data/outra.txt" in job
