#!/usr/bin/env bash
# Qualquer subcomando do buscador com as credenciais do KyX já carregadas
# (`python -m buscador_midias <subcomando> ...`). Os argumentos vão direto ao comando.
#
# Uso:
#   scripts/bm.sh dashboard --port 8765
#   scripts/bm.sh report
#   scripts/bm.sh migrate-sqlite var/state.db
#
# Credenciais: lidas do .env do KyX (KYX_ENV, padrão ../KyX-NovoDD-backend/.env), só as variáveis
# que o buscador usa (conexão com o Postgres incluída) e sem imprimir nada. O que já estiver no ambiente vence o arquivo. O corpus
# é o Postgres local (DB_MIDIAS_IA_HOST=127.0.0.1); troque com CORPUS_DB_HOST.
#
# Variáveis úteis (testes): PY (interpretador), KYX_ENV (arquivo de credenciais).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KYX_ENV="${KYX_ENV:-$ROOT/../KyX-NovoDD-backend/.env}"
PY="${PY:-$ROOT/.venv/bin/python}"

# O serviço de ingestão usa seu broker exclusivo também quando iniciado pelo terminal.
# Importa apenas as seis chaves conhecidas, sem executar conteúdo do arquivo.
if [ "${1:-}" = ingest ] && [ -f "$ROOT/var/ingestion/rabbit.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    key="${line%%=*}"
    case "$key" in
      RABBITMQ_HOST|RABBITMQ_PORT|RABBITMQ_USER|RABBITMQ_PASSWORD|RABBITMQ_VHOST|BM_QUEUE_PREFIX|BM_INGEST_WORK_DIR)
        [ -n "${!key+x}" ] || export "$key=${line#*=}"
        ;;
    esac
  done < "$ROOT/var/ingestion/rabbit.env"
fi

if [ ! -f "$KYX_ENV" ]; then
  echo "arquivo de credenciais não encontrado: $KYX_ENV" >&2
  exit 2
fi

ALLOWED=" BRIGHTDATA_API_KEY BRIGHTDATA_ZONE_UNLOCKER BRIGHTDATA_BASE_URL OPENROUTER_API_KEY
 OPENROUTER_BASE_URL MIDIAS_IA_STORAGE_DISK AWS_BUCKET AWS_ENDPOINT AWS_DEFAULT_REGION
 AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_USE_PATH_STYLE_ENDPOINT DB_MIDIAS_IA_PORT
 DB_MIDIAS_IA_DATABASE DB_MIDIAS_IA_USERNAME DB_MIDIAS_IA_PASSWORD
 RABBITMQ_HOST RABBITMQ_PORT RABBITMQ_USER RABBITMQ_PASSWORD RABBITMQ_VHOST "
ALLOWED="$(echo "$ALLOWED" | tr '\n' ' ')"

# Lê linha a linha (nunca com eval): o valor é só texto, mesmo que traga $(...) ou crases.
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in '' | '#'*) continue ;; esac
  key="${line%%=*}"
  value="${line#*=}"
  case "$ALLOWED" in *" $key "*) ;; *) continue ;; esac
  [ -n "${!key+x}" ] && continue
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  export "$key=$value"
done < "$KYX_ENV"
export DB_MIDIAS_IA_HOST="${CORPUS_DB_HOST:-127.0.0.1}"

cd "$ROOT"
exec "$PY" -m buscador_midias "$@"
