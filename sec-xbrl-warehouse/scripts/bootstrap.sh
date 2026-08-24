#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3.12}"
VENV="${VENV:-.venv}"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env. Set XBRL_SEC_USER_AGENT before any SEC network request."
fi

"${PYTHON_BIN}" -m venv "${VENV}"
"${VENV}/bin/python" -m pip install --upgrade pip
"${VENV}/bin/python" -m pip install -e '.[dev]'
docker compose up -d postgres clickhouse minio minio-init
"${VENV}/bin/alembic" upgrade head
"${VENV}/bin/xbrl-warehouse" db init-clickhouse
"${VENV}/bin/xbrl-warehouse" doctor --skip-sec-identity

echo "Bootstrap complete. Edit .env and run: ${VENV}/bin/xbrl-warehouse doctor"
