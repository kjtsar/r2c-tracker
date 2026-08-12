#!/bin/sh
set -eu

SKIP_UNIT_TESTS=0
if [ "${1:-}" = "--skip-unit-tests" ]; then
  SKIP_UNIT_TESTS=1
  shift
fi
if [ "$#" -ne 0 ]; then
  echo "Usage: $0 [--skip-unit-tests]" >&2
  exit 2
fi

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "${ROOT_DIR}"

PYTHON="${PYTHON:-.venv/bin/python}"
OUTPUT_DIR="${SECURITY_OUTPUT_DIR:-security-artifacts}"
TMP_ROOT="${TMPDIR:-/tmp}"
TMP_DIR="${TMP_ROOT%/}/r2c-tracker-security-check.$$"
AUDIT_LOG="${TMP_DIR}/dependency-audit.log"
BANDIT_LOG="${TMP_DIR}/bandit.log"
SECRETS_LOG="${TMP_DIR}/secret-scan.log"
SBOM_LOG="${TMP_DIR}/sbom.log"
AUDIT_PID=""
BANDIT_PID=""
SECRETS_PID=""
SBOM_PID=""

cleanup() {
  for pid in "${AUDIT_PID}" "${BANDIT_PID}" "${SECRETS_PID}" "${SBOM_PID}"; do
    if [ -n "${pid}" ] && kill -0 "${pid}" >/dev/null 2>&1; then
      kill "${pid}" >/dev/null 2>&1 || true
    fi
  done
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT INT TERM

PYTHON_PATH="$(command -v "${PYTHON}" 2>/dev/null || true)"
if [ -z "${PYTHON_PATH}" ] || [ ! -x "${PYTHON_PATH}" ]; then
  echo "Python runtime not found at ${PYTHON}." >&2
  exit 2
fi
PYTHON="${PYTHON_PATH}"

for module in pip_audit bandit detect_secrets cyclonedx_py; do
  if ! "${PYTHON}" -c "import ${module}" >/dev/null 2>&1; then
    echo "Security tools are missing. Install requirements-security.txt in the selected environment." >&2
    exit 2
  fi
done

mkdir -p "${OUTPUT_DIR}" "${TMP_DIR}"

echo "==> Authorization inventory and regression tests"
if [ "${SKIP_UNIT_TESTS}" -eq 1 ]; then
  echo "Already completed by the combined release qualification workflow."
else
  "${PYTHON}" -m unittest discover -s tests -p "test_*.py"
fi

echo "==> Independent security analyses (parallel)"
(
  echo "Dependency vulnerability audit"
  "${PYTHON}" -m pip_audit -r requirements.lock --no-deps --disable-pip
) >"${AUDIT_LOG}" 2>&1 &
AUDIT_PID="$!"
(
  echo "Static security analysis"
  "${PYTHON}" -m bandit -q -ll -r \
    main.py control_plane.py enrollment.py faa_proxy.py \
    platform_admin.py platform_admin_auth.py platform_admin_identity.py \
    stripe_checkout.py turn_credentials.py \
    scripts/create_release_check_credential.py scripts/release_guard.py
) >"${BANDIT_LOG}" 2>&1 &
BANDIT_PID="$!"
(
  echo "Tracked-source secret scan"
  git ls-files -z -- ':!.secrets.baseline' \
    | xargs -0 "${PYTHON}" -m detect_secrets.pre_commit_hook \
    --baseline .secrets.baseline
  git ls-files -z -- ':!.secrets.baseline' \
    | xargs -0 "${PYTHON}" -m detect_secrets scan \
    > "${OUTPUT_DIR}/detect-secrets.json"
) >"${SECRETS_LOG}" 2>&1 &
SECRETS_PID="$!"
(
  echo "CycloneDX SBOM"
  "${PYTHON}" -m cyclonedx_py requirements requirements.lock \
    --output-format JSON \
    --output-file "${OUTPUT_DIR}/r2c-tracker.cdx.json"
) >"${SBOM_LOG}" 2>&1 &
SBOM_PID="$!"

set +e
wait "${AUDIT_PID}"
audit_status="$?"
AUDIT_PID=""
wait "${BANDIT_PID}"
bandit_status="$?"
BANDIT_PID=""
wait "${SECRETS_PID}"
secrets_status="$?"
SECRETS_PID=""
wait "${SBOM_PID}"
sbom_status="$?"
SBOM_PID=""
set -e

for log in "${AUDIT_LOG}" "${BANDIT_LOG}" "${SECRETS_LOG}" "${SBOM_LOG}"; do
  cat "${log}"
done

if [ "${audit_status}" -ne 0 ] \
    || [ "${bandit_status}" -ne 0 ] \
    || [ "${secrets_status}" -ne 0 ] \
    || [ "${sbom_status}" -ne 0 ]; then
  echo "Security checks failed: audit=${audit_status} bandit=${bandit_status} secrets=${secrets_status} sbom=${sbom_status}." >&2
  exit 1
fi

echo "Security artifacts written to ${OUTPUT_DIR}."
