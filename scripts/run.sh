#!/usr/bin/env bash
# O fluxo todo, para o agendador: descobrir, analisar, conferir o critério, capturar a evidência
# e gravar no corpus (`python -m buscador_midias run`). Os argumentos vão direto ao comando.
#
# Uso:
#   scripts/run.sh [opções do `run`]        ex.: scripts/run.sh --only-file data/poc_sample.txt
#
# As credenciais vêm do .env do KyX (veja scripts/bm.sh, que faz o trabalho).
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bm.sh" run "$@"
