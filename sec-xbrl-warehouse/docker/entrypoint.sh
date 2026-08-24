#!/usr/bin/env sh
set -eu

mkdir -p "${XBRL_DATA_DIR:-/app/data}" "${XBRL_TEMP_DIR:-/app/data/tmp}"
exec xbrl-warehouse "$@"
