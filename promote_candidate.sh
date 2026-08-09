#!/bin/sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="${PYTHON:-${SCRIPT_DIR}/.venv/bin/python}"
exec "${PYTHON}" "${SCRIPT_DIR}/scripts/release_guard.py" promote "$@"
