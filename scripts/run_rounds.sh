#!/usr/bin/env bash
# Rodadas espaçadas da descoberta (POC), com triagem e backup opcionais no fim.
#
# Uso:
#   scripts/run_rounds.sh [opções]
#     --count N            número de rodadas (padrão 5)
#     --first-delay SEG    espera antes da 1ª rodada (padrão 3600)
#     --interval SEG       espera entre as rodadas, contada do fim da anterior (padrão 7200)
#     --sample ARQUIVO     fontes a varrer (padrão data/poc_sample.txt)
#     --triage-each-round  após cada rodada, roda a triagem por LLM nas URLs novas pendentes
#     --enrich-each-round  após cada rodada (e da triagem), baixa o texto, analisa o contexto e
#                          captura a evidência dos achados (reaproveita o HTML se for confiável)
#     --corpus-each-round  após cada rodada (e do enriquecimento), grava os achados no corpus de
#                          Mídias IA (Postgres; exige as DB_MIDIAS_IA_* no .env do KyX)
#     --triage-at-end      ao final, roda a triagem por LLM nas pendentes (resgata falhas)
#     --backup-at-end      ao final, grava um backup do estado (var/backups/)
#     --write-schedule     grava var/schedule.json com as execuções previstas (agenda do painel)
#     --dry-run            só mostra o plano; não espera, não acessa a rede, não gasta créditos
#
# Credenciais: lidas do .env do KyX (KYX_ENV, padrão ../KyX-NovoDD-backend/.env) sem imprimir
# nada. Bright Data é sempre usado como fallback (teto de 60 chamadas por rodada). A triagem exige
# OPENROUTER_API_KEY no mesmo .env.
#
# Variáveis úteis (testes): START_EPOCH (instante inicial), SCHEDULE_FILE (destino da agenda).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COUNT=5
FIRST_DELAY=3600
INTERVAL=7200
SAMPLE="data/poc_sample.txt"
TRIAGE_AT_END=0
TRIAGE_EACH_ROUND=0
ENRICH_EACH_ROUND=0
CORPUS_EACH_ROUND=0
BACKUP_AT_END=0
WRITE_SCHEDULE=0
DRY_RUN=0
KYX_ENV="${KYX_ENV:-$ROOT/../KyX-NovoDD-backend/.env}"
SCHEDULE_FILE="${SCHEDULE_FILE:-$ROOT/var/schedule.json}"
START_EPOCH="${START_EPOCH:-$(date +%s)}"
PY="$ROOT/.venv/bin/python"

while [ $# -gt 0 ]; do
  case "$1" in
    --count) COUNT="$2"; shift 2 ;;
    --first-delay) FIRST_DELAY="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --sample) SAMPLE="$2"; shift 2 ;;
    --triage-at-end) TRIAGE_AT_END=1; shift ;;
    --triage-each-round) TRIAGE_EACH_ROUND=1; shift ;;
    --enrich-each-round) ENRICH_EACH_ROUND=1; shift ;;
    --corpus-each-round) CORPUS_EACH_ROUND=1; shift ;;
    --backup-at-end) BACKUP_AT_END=1; shift ;;
    --write-schedule) WRITE_SCHEDULE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "opção desconhecida: $1" >&2; exit 2 ;;
  esac
done

iso() { date -d "@$1" --iso-8601=seconds; }
stamp() { date -d "@$1" '+%F %T'; }

# Início previsto de cada rodada (a deriva de ~1-2 min por rodada não é modelada).
plan_epoch() { echo $((START_EPOCH + FIRST_DELAY + ($1 - 1) * INTERVAL)); }
END_EPOCH=$(plan_epoch "$COUNT")
ROUND_TRIAGE_OFFSET=120   # a triagem de cada rodada começa ~2 min depois do início dela
ROUND_ENRICH_OFFSET=300   # o enriquecimento começa ~5 min depois, quando a triagem já terminou
ROUND_CORPUS_OFFSET=900   # a gravação no corpus começa ~15 min depois, quando o enriquecimento acabou
END_TRIAGE_OFFSET=180

echo "== plano: $COUNT rodada(s), amostra $SAMPLE"
for i in $(seq 1 "$COUNT"); do
  echo "rodada $i/$COUNT prevista para $(stamp "$(plan_epoch "$i")")"
  if [ "$TRIAGE_EACH_ROUND" = 1 ]; then
    echo "triagem após a rodada $i/$COUNT prevista para $(stamp $(($(plan_epoch "$i") + ROUND_TRIAGE_OFFSET)))"
  fi
  if [ "$ENRICH_EACH_ROUND" = 1 ]; then
    echo "enriquecimento após a rodada $i/$COUNT prevista para $(stamp $(($(plan_epoch "$i") + ROUND_ENRICH_OFFSET)))"
  fi
  if [ "$CORPUS_EACH_ROUND" = 1 ]; then
    echo "gravação no corpus após a rodada $i/$COUNT prevista para $(stamp $(($(plan_epoch "$i") + ROUND_CORPUS_OFFSET)))"
  fi
done
[ "$TRIAGE_AT_END" = 1 ] && echo "triagem final prevista para $(stamp $((END_EPOCH + END_TRIAGE_OFFSET)))"
[ "$BACKUP_AT_END" = 1 ] && echo "backup previsto para o fim"

if [ "$WRITE_SCHEDULE" = 1 ]; then
  mkdir -p "$(dirname "$SCHEDULE_FILE")"
  jobs=()
  for i in $(seq 1 "$COUNT"); do
    jobs+=("$(printf '{"name": "Rodada agendada %s/%s", "kind": "discovery", "at": "%s", "note": "início previsto"}' \
      "$i" "$COUNT" "$(iso "$(plan_epoch "$i")")")")
    if [ "$TRIAGE_EACH_ROUND" = 1 ]; then
      jobs+=("$(printf '{"name": "Triagem após a rodada %s/%s", "kind": "triage", "at": "%s", "note": "veredito das novas da rodada"}' \
        "$i" "$COUNT" "$(iso $(($(plan_epoch "$i") + ROUND_TRIAGE_OFFSET)))")")
    fi
    if [ "$ENRICH_EACH_ROUND" = 1 ]; then
      jobs+=("$(printf '{"name": "Enriquecimento após a rodada %s/%s", "kind": "enrich", "at": "%s", "note": "texto, contexto e evidência dos mantidos"}' \
        "$i" "$COUNT" "$(iso $(($(plan_epoch "$i") + ROUND_ENRICH_OFFSET)))")")
    fi
    if [ "$CORPUS_EACH_ROUND" = 1 ]; then
      jobs+=("$(printf '{"name": "Gravação no corpus após a rodada %s/%s", "kind": "corpus", "at": "%s", "note": "achados com evidência vão para o Postgres"}' \
        "$i" "$COUNT" "$(iso $(($(plan_epoch "$i") + ROUND_CORPUS_OFFSET)))")")
    fi
  done
  if [ "$TRIAGE_AT_END" = 1 ]; then
    jobs+=("$(printf '{"name": "Triagem final (resgate)", "kind": "triage", "at": "%s", "note": "após a última rodada"}' \
      "$(iso $((END_EPOCH + END_TRIAGE_OFFSET)))")")
  fi
  {
    printf '{"jobs": [\n'
    last=$((${#jobs[@]} - 1))
    for idx in "${!jobs[@]}"; do
      sep=","; [ "$idx" = "$last" ] && sep=""
      printf '  %s%s\n' "${jobs[$idx]}" "$sep"
    done
    printf ']}\n'
  } > "$SCHEDULE_FILE"
  echo "agenda gravada em $SCHEDULE_FILE"
fi

[ "$DRY_RUN" = 1 ] && exit 0

# Evita dois agendadores ao mesmo tempo disputando o mesmo estado.
mkdir -p "$ROOT/var"
exec 9>"$ROOT/var/rounds.lock"
flock -n 9 || { echo "já existe um run_rounds.sh em execução" >&2; exit 3; }

cd "$ROOT"
set -a
# shellcheck disable=SC1090
eval "$(grep -E '^(BRIGHTDATA_API_KEY|BRIGHTDATA_ZONE_UNLOCKER|BRIGHTDATA_BASE_URL|OPENROUTER_API_KEY|OPENROUTER_BASE_URL|MIDIAS_IA_STORAGE_DISK|AWS_(BUCKET|ENDPOINT|DEFAULT_REGION|ACCESS_KEY_ID|SECRET_ACCESS_KEY|USE_PATH_STYLE_ENDPOINT)|DB_MIDIAS_IA_(PORT|DATABASE|USERNAME|PASSWORD))=' "$KYX_ENV")"
set +a
export DB_MIDIAS_IA_HOST=127.0.0.1

# Triagem por LLM das URLs novas pendentes. Roda separada da descoberta para que um problema com o
# provedor (chave, rede, cota) nunca perca o resultado da rodada: o que falhar fica pendente e é
# retomado na próxima triagem.
triage_step() {
  local rc=0 output
  output=$("$PY" -m buscador_midias triage 2>&1) || rc=$?
  echo "$output" | tail -n 8
  if [ "$rc" != 0 ]; then echo "ERRO: a triagem terminou com código $rc"; fi
}

# Enriquecimento: texto por HTTP direto, análise de contexto e evidência dos achados. Roda separado
# para que uma falha (OpenRouter, rede, Bright Data) nunca perca a rodada nem a triagem; o que não
# for concluído continua pendente e é retomado na próxima.
enrich_step() {
  local rc=0 output
  output=$("$PY" -m buscador_midias enrich --bright-data-max-requests 100 2>&1) || rc=$?
  echo "$output" | tail -n 14
  if [ "$rc" != 0 ]; then echo "ERRO: o enriquecimento terminou com código $rc"; fi
}

# Gravação dos achados no corpus de Mídias IA. Separada para que um problema com o Postgres nunca
# perca a rodada, a triagem nem o enriquecimento: o que não for gravado continua pendente e a
# próxima execução retoma (a gravação é idempotente).
corpus_step() {
  local rc=0 output
  output=$("$PY" -m buscador_midias corpus-sync 2>&1) || rc=$?
  echo "$output" | tail -n 10
  if [ "$rc" != 0 ]; then echo "ERRO: a gravação no corpus terminou com código $rc"; fi
}

for i in $(seq 1 "$COUNT"); do
  if [ "$i" = 1 ]; then sleep "$FIRST_DELAY"; else sleep "$INTERVAL"; fi
  echo "== RODADA $i/$COUNT $(date '+%F %T')"
  rc=0
  output=$(timeout 600 "$PY" -m buscador_midias discover --only-file "$SAMPLE" \
    --bright-data --bright-data-max-requests 60 --corpus --progress-every 25 2>&1) || rc=$?
  echo "$output" | grep -E "rodada|novas|backlog|filtradas|falhas|lidos|pulados|bright_data_requests" || true
  if [ "$rc" != 0 ]; then
    # Uma rodada que falha não derruba as seguintes, mas o motivo precisa aparecer no log.
    echo "ERRO: a rodada $i terminou com código $rc"
    echo "$output" | tail -n 5
  fi
  if [ "$TRIAGE_EACH_ROUND" = 1 ]; then
    echo "== TRIAGEM após a rodada $i/$COUNT $(date '+%F %T')"
    triage_step
  fi
  if [ "$ENRICH_EACH_ROUND" = 1 ]; then
    echo "== ENRIQUECIMENTO após a rodada $i/$COUNT $(date '+%F %T')"
    enrich_step
  fi
  if [ "$CORPUS_EACH_ROUND" = 1 ]; then
    echo "== CORPUS após a rodada $i/$COUNT $(date '+%F %T')"
    corpus_step
  fi
done

if [ "$TRIAGE_AT_END" = 1 ]; then
  echo "== TRIAGEM FINAL $(date '+%F %T')"
  triage_step
fi
if [ "$BACKUP_AT_END" = 1 ]; then
  echo "== BACKUP $(date '+%F %T')"
  "$PY" -m buscador_midias backup
fi
echo "== RELATORIO"
"$PY" -m buscador_midias report 2>&1 | cut -c1-260
echo "== FIM $(date '+%F %T')"
