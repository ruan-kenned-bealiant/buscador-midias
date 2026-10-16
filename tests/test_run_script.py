import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run.sh"

ENV_FILE = """\
# credenciais do KyX
OPENROUTER_API_KEY=sk-or-segredo
BRIGHTDATA_API_KEY="bd-segredo"
BRIGHTDATA_ZONE_UNLOCKER='zona'
DB_MIDIAS_IA_HOST=pgsql_midias
DB_MIDIAS_IA_PASSWORD=senha com espaço $(touch /tmp/NAO-DEVE-EXECUTAR)
APP_KEY=base64:nao-deve-vazar
OPENROUTER_BASE_URL=
"""

FAKE_PYTHON = """\
#!/usr/bin/env bash
echo "ARGS:$*"
for name in OPENROUTER_API_KEY BRIGHTDATA_API_KEY BRIGHTDATA_ZONE_UNLOCKER DB_MIDIAS_IA_HOST \
            DB_MIDIAS_IA_PASSWORD APP_KEY; do
  echo "$name=${!name-<ausente>}"
done
"""


def make_python(tmp_path: Path) -> Path:
    fake = tmp_path / "python"
    fake.write_text(FAKE_PYTHON, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def run(tmp_path: Path, *args: str, env_file: Path | None = None, **extra: str):
    env_path = env_file or tmp_path / "kyx.env"
    if env_file is None:
        env_path.write_text(ENV_FILE, encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"OPENROUTER_API_KEY", "APP_KEY", "BRIGHTDATA_API_KEY"}
    }
    environment.update(PY=str(make_python(tmp_path)), KYX_ENV=str(env_path), **extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def test_repassa_os_argumentos_ao_comando_run(tmp_path: Path):
    result = run(tmp_path, "--only-file", "data/poc_sample.txt")

    assert result.returncode == 0
    assert "ARGS:-m buscador_midias run --only-file data/poc_sample.txt" in result.stdout


def test_carrega_so_as_credenciais_necessarias_e_tira_as_aspas(tmp_path: Path):
    lines = run(tmp_path).stdout.splitlines()

    assert "OPENROUTER_API_KEY=sk-or-segredo" in lines
    assert "BRIGHTDATA_API_KEY=bd-segredo" in lines
    assert "BRIGHTDATA_ZONE_UNLOCKER=zona" in lines
    assert "APP_KEY=<ausente>" in lines  # o que não é do buscador nunca é exportado


def test_o_corpus_e_o_banco_local_por_padrao_e_pode_ser_trocado(tmp_path: Path):
    assert "DB_MIDIAS_IA_HOST=127.0.0.1" in run(tmp_path).stdout.splitlines()

    other = run(tmp_path, CORPUS_DB_HOST="pg.interno")

    assert "DB_MIDIAS_IA_HOST=pg.interno" in other.stdout.splitlines()


def test_valor_do_env_nunca_e_executado_como_comando(tmp_path: Path):
    marker = Path("/tmp/NAO-DEVE-EXECUTAR")
    marker.unlink(missing_ok=True)

    result = run(tmp_path)

    assert not marker.exists()
    assert "DB_MIDIAS_IA_PASSWORD=senha com espaço $(touch /tmp/NAO-DEVE-EXECUTAR)" in (
        result.stdout.splitlines()
    )


def test_variavel_ja_definida_no_ambiente_vence_o_arquivo(tmp_path: Path):
    result = run(tmp_path, OPENROUTER_API_KEY="do-ambiente")

    assert "OPENROUTER_API_KEY=do-ambiente" in result.stdout.splitlines()


def test_sem_arquivo_de_credenciais_falha_com_mensagem_e_sem_rodar_nada(tmp_path: Path):
    result = run(tmp_path, env_file=tmp_path / "nao-existe.env")

    assert result.returncode == 2
    assert "nao-existe.env" in result.stderr
    assert "ARGS" not in result.stdout
