#!/usr/bin/env bash
# Create or update the least-privilege identities the Compose services use.
#
#   PostgreSQL  xbrl_worker (DML only), xbrl_api (CONNECT only)
#   ClickHouse  xbrl_writer (SELECT, INSERT), xbrl_api (SELECT, budgeted profile)
#   MinIO       created by the minio-init service (no delete permission)
#
# Run from sec-xbrl-warehouse/ after `docker compose up -d postgres clickhouse`
# and the Alembic migrations. Idempotent: rerun it after changing a password.
# Passwords come from the environment or .env; the defaults are development
# values, which XBRL_ENV=production rejects.
set -euo pipefail

cd "$(dirname "$0")/../.."
# Read KEY=VALUE lines from .env as data (values may contain spaces); variables
# already set in the environment win.
if [ -f .env ]; then
    while IFS='=' read -r key value; do
        case "$key" in '' | \#*) continue ;; esac
        if [ -z "${!key+x}" ]; then export "$key=$value"; fi
    done < .env
fi
if docker compose version >/dev/null 2>&1; then compose=(docker compose); else compose=(docker-compose); fi

: "${POSTGRES_USER:=xbrl}" "${POSTGRES_DB:=xbrl_control}" "${CLICKHOUSE_USER:=xbrl}"
: "${CLICKHOUSE_DB:=xbrl}" "${CLICKHOUSE_PASSWORD:=xbrl_dev_password}"
: "${XBRL_WORKER_POSTGRES_PASSWORD:=xbrl_worker_dev_password}"
: "${XBRL_API_POSTGRES_PASSWORD:=xbrl_api_dev_password}"
: "${XBRL_WRITER_CLICKHOUSE_PASSWORD:=xbrl_writer_dev_password}"
: "${XBRL_API_CLICKHOUSE_PASSWORD:=xbrl_api_dev_password}"

"${compose[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -q \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v owner="$POSTGRES_USER" -v db="$POSTGRES_DB" \
    -v worker_password="$XBRL_WORKER_POSTGRES_PASSWORD" \
    -v api_password="$XBRL_API_POSTGRES_PASSWORD" \
    < ops/provision/postgres_roles.sql
echo "PostgreSQL: xbrl_worker, xbrl_api"

quote() {  # ClickHouse string literal
    local value=${1//\\/\\\\}
    printf "'%s'" "${value//\'/\\\'}"
}
db=$CLICKHOUSE_DB
"${compose[@]}" exec -T clickhouse clickhouse-client --multiquery \
    --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" <<SQL
CREATE USER IF NOT EXISTS xbrl_writer IDENTIFIED WITH sha256_password BY $(quote "$XBRL_WRITER_CLICKHOUSE_PASSWORD");
ALTER USER xbrl_writer IDENTIFIED WITH sha256_password BY $(quote "$XBRL_WRITER_CLICKHOUSE_PASSWORD");
GRANT SELECT, INSERT ON ${db}.* TO xbrl_writer;

-- The API checks these effective limits at startup and refuses to start when any
-- is missing or looser than its configuration (SECURITY.md). readonly = 1 fixes
-- them all: the reader can neither write nor change a setting.
CREATE SETTINGS PROFILE IF NOT EXISTS xbrl_api_profile;
ALTER SETTINGS PROFILE xbrl_api_profile SETTINGS
    readonly = 1,
    max_execution_time = ${XBRL_API_QUERY_TIMEOUT_SECONDS:-30},
    timeout_before_checking_execution_speed = 0,
    max_memory_usage = ${XBRL_API_QUERY_MAX_MEMORY_BYTES:-536870912},
    max_rows_to_read = ${XBRL_API_QUERY_MAX_ROWS:-5000000},
    max_bytes_to_read = ${XBRL_API_QUERY_MAX_BYTES:-1073741824},
    max_result_rows = 10000,
    max_result_bytes = ${XBRL_API_RESULT_MAX_BYTES:-16777216},
    result_overflow_mode = 'throw',
    read_overflow_mode = 'throw',
    timeout_overflow_mode = 'throw',
    max_threads = 2;
CREATE USER IF NOT EXISTS xbrl_api IDENTIFIED WITH sha256_password BY $(quote "$XBRL_API_CLICKHOUSE_PASSWORD");
ALTER USER xbrl_api IDENTIFIED WITH sha256_password BY $(quote "$XBRL_API_CLICKHOUSE_PASSWORD")
    SETTINGS PROFILE 'xbrl_api_profile';
GRANT SELECT ON ${db}.* TO xbrl_api;
SQL
echo "ClickHouse: xbrl_writer, xbrl_api (profile xbrl_api_profile)"
echo "MinIO: run '${compose[*]} up minio-init' to create xbrl_worker"
