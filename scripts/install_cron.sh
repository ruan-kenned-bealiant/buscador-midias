#!/usr/bin/env bash
# Instala (ou remove) no cron do usuário os horários em que o fluxo roda todo dia, e grava a agenda
# que o painel mostra. Idempotente: o bloco do buscador é reescrito e o resto do crontab fica como
# está.
#
# Uso:
#   scripts/install_cron.sh [--times 06:00,12:00,16:00] [--sample data/poc_sample.txt]
#                           [--extra "--max-sitemap-fetches 12"]
#   scripts/install_cron.sh --remove
#   scripts/install_cron.sh --dry-run ...      só mostra o crontab resultante
#
# Os horários valem no fuso da máquina (é o do cron). Variável útil (testes): SCHEDULE_FILE.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIMES="06:00,12:00,16:00"
SAMPLE="data/poc_sample.txt"
EXTRA=""
SCHEDULE_FILE="${SCHEDULE_FILE:-$ROOT/var/schedule.json}"
REMOVE=0
DRY_RUN=0
BEGIN="# >>> buscador-midias >>>"
END="# <<< buscador-midias <<<"

while [ $# -gt 0 ]; do
  case "$1" in
    --times) TIMES="$2"; shift 2 ;;
    --sample) SAMPLE="$2"; shift 2 ;;
    --extra) EXTRA="$2"; shift 2 ;;
    --remove) REMOVE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "opção desconhecida: $1" >&2; exit 2 ;;
  esac
done

# O crontab atual sem o bloco do buscador (um crontab vazio não é erro).
current="$( (crontab -l 2>/dev/null || true) | awk -v b="$BEGIN" -v e="$END" '
  $0 == b { skip = 1; next }
  $0 == e { skip = 0; next }
  !skip { print }')"

block=""
if [ "$REMOVE" = 0 ]; then
  IFS=',' read -r -a clocks <<< "$TIMES"
  lines=()
  for clock in "${clocks[@]}"; do
    if [[ ! "$clock" =~ ^([01][0-9]|2[0-3]):([0-5][0-9])$ ]]; then
      echo "horário inválido: '$clock' (use HH:MM, ex.: 06:00)" >&2
      exit 2
    fi
    hour=$((10#${clock%%:*}))
    minute=$((10#${clock##*:}))
    command="scripts/run.sh --only-file $SAMPLE"
    [ -n "$EXTRA" ] && command="$command $EXTRA"
    lines+=("$minute $hour * * * cd $ROOT && $command >> var/run.log 2>&1")
  done
  block="$BEGIN"$'\n'"$(printf '%s\n' "${lines[@]}")"$'\n'"$END"
fi

if [ -n "$current" ] && [ -n "$block" ]; then
  result="$current"$'\n'"$block"
elif [ -n "$block" ]; then
  result="$block"
else
  result="$current"
fi

if [ "$DRY_RUN" = 1 ]; then
  printf '%s\n' "$result"
  exit 0
fi

printf '%s\n' "$result" | crontab -

if [ "$REMOVE" = 0 ]; then
  mkdir -p "$(dirname "$SCHEDULE_FILE")"
  daily="$(printf '"%s", ' "${clocks[@]}")"
  printf '{"daily": [%s]}\n' "${daily%, }" > "$SCHEDULE_FILE"
  echo "instalado: $TIMES (agenda do painel em $SCHEDULE_FILE)"
else
  echo "removido do crontab"
fi
