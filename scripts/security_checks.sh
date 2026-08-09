#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "${ROOT_DIR}"

PYTHON="${PYTHON:-.venv/bin/python}"
OUTPUT_DIR="${SECURITY_OUTPUT_DIR:-security-artifacts}"

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

mkdir -p "${OUTPUT_DIR}"

echo "==> Authorization inventory and regression tests"
"${PYTHON}" -m unittest discover -s tests -p "test_*.py"

echo "==> Dependency vulnerability audit"
"${PYTHON}" -m pip_audit -r requirements.lock --no-deps --disable-pip

echo "==> Static security analysis"
"${PYTHON}" -m bandit -q -ll -r \
  main.py control_plane.py enrollment.py faa_proxy.py \
  platform_admin.py platform_admin_auth.py platform_admin_identity.py \
  stripe_checkout.py turn_credentials.py scripts/release_guard.py

echo "==> Tracked-source secret scan"
git ls-files -z -- ':!.secrets.baseline' \
  | xargs -0 "${PYTHON}" -m detect_secrets.pre_commit_hook \
  --baseline .secrets.baseline
git ls-files -z -- ':!.secrets.baseline' \
  | xargs -0 "${PYTHON}" -m detect_secrets scan \
  > "${OUTPUT_DIR}/detect-secrets.json"

echo "==> CycloneDX SBOM"
"${PYTHON}" -m cyclonedx_py requirements requirements.lock \
  --output-format JSON \
  --output-file "${OUTPUT_DIR}/r2c-tracker.cdx.json"

echo "Security artifacts written to ${OUTPUT_DIR}."
