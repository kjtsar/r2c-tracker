#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "${ROOT_DIR}"

PYTHON="${PYTHON:-.venv/bin/python}"
TMP_ROOT="${TMPDIR:-/tmp}"
TMP_DIR="${TMP_ROOT%/}/r2c-tracker-qualification.$$"
RELEASE_LOG="${TMP_DIR}/release-check.log"
SECURITY_LOG="${TMP_DIR}/security-checks.log"
RELEASE_PID=""
SECURITY_PID=""

cleanup() {
  for pid in "${RELEASE_PID}" "${SECURITY_PID}"; do
    if [ -n "${pid}" ] && kill -0 "${pid}" >/dev/null 2>&1; then
      kill "${pid}" >/dev/null 2>&1 || true
    fi
  done
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT INT TERM

if [ ! -x "${PYTHON}" ]; then
  echo "Python runtime not found at ${PYTHON}." >&2
  exit 2
fi

mkdir -p "${TMP_DIR}"
started_at="$(date +%s)"

echo "==> Complete unit suite (shared by runtime and security gates)"
"${PYTHON}" -m unittest discover -s tests -p "test_*.py"

echo "==> Runtime/migration and security gates (parallel)"
./release_check.sh --skip-unit-tests >"${RELEASE_LOG}" 2>&1 &
RELEASE_PID="$!"
./scripts/security_checks.sh --skip-unit-tests >"${SECURITY_LOG}" 2>&1 &
SECURITY_PID="$!"

set +e
wait "${RELEASE_PID}"
release_status="$?"
RELEASE_PID=""
wait "${SECURITY_PID}"
security_status="$?"
SECURITY_PID=""
set -e

echo "==> Runtime/migration gate output"
cat "${RELEASE_LOG}"
echo "==> Security gate output"
cat "${SECURITY_LOG}"

if [ "${release_status}" -ne 0 ] || [ "${security_status}" -ne 0 ]; then
  echo "Release qualification failed: runtime=${release_status} security=${security_status}." >&2
  exit 1
fi

finished_at="$(date +%s)"
echo "Release qualification passed in $((finished_at - started_at)) seconds."
"${PYTHON}" scripts/release_guard.py record-qualification
