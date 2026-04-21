#!/bin/sh
set -eu

: "${DATABASE_URL:?DATABASE_URL must be set in the environment}"
: "${TRACKER_ADMIN_PASS:?TRACKER_ADMIN_PASS must be set in the environment}"
: "${TRACKER_API_KEY:?TRACKER_API_KEY must be set in the environment}"

validate_database_url() {
  python3 - <<'PY'
import os
import sys
from urllib.parse import urlsplit

url = os.environ["DATABASE_URL"]
try:
    parts = urlsplit(url)
except ValueError as exc:
    sys.stderr.write(f"DATABASE_URL is not parseable: {exc}\n")
    sys.stderr.write("Current value starts with: " + url[:80] + "\n")
    sys.stderr.write(
        "This usually means the password contains reserved URL characters like @, :, /, ?, #, [, ], or % "
        "and needs to be percent-encoded.\n"
    )
    sys.exit(1)

errors = []
if not parts.scheme:
    errors.append("missing URL scheme")
if not parts.hostname:
    errors.append("missing hostname")
if parts.port is None:
    errors.append("missing numeric port")
if not parts.path or parts.path == "/":
    errors.append("missing database name in path")

if errors:
    sys.stderr.write("DATABASE_URL does not look valid: " + ", ".join(errors) + ".\n")
    sys.stderr.write("Current value starts with: " + url[:80] + "\n")
    sys.stderr.write(
        "If your DB password contains reserved URL characters like @, :, /, ?, #, [, ], or %, "
        "it must be percent-encoded inside DATABASE_URL.\n"
    )
    sys.exit(1)
PY
}

validate_database_url

REGION="${REGION:-us-west1}"
SERVICE_NAME="${SERVICE_NAME:-r2c-tracker}"
SECRET_KEY_SECRET_NAME="${SECRET_KEY_SECRET_NAME:-r2c-tracker-secret-key}"
TRACKER_VERSION="${TRACKER_VERSION:-$(git describe --tags --always 2>/dev/null || echo unknown)}"
TRACKER_RECENT_VERSIONS="${TRACKER_RECENT_VERSIONS:-$(python3 - <<'PY'
import json
import subprocess

def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()

try:
    tags = run(["git", "tag", "--sort=-creatordate"]).splitlines()
    versions = []
    for tag in [tag.strip() for tag in tags if tag.strip()][:10]:
        versions.append({
            "tag": tag,
            "date": run(["git", "log", "-1", "--date=short", "--format=%ad", tag]),
            "summary": run(["git", "log", "-1", "--format=%s", tag]),
        })
    print(json.dumps(versions))
except Exception:
    print("[]")
PY
)}"
GCLOUD_PROJECT="${GCLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
GCLOUD_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n 1 || true)"

export TRACKER_VERSION
export TRACKER_RECENT_VERSIONS

: "${GCLOUD_PROJECT:?No active gcloud project found. Run 'gcloud config set project YOUR_PROJECT_ID' or export GCLOUD_PROJECT.}"
: "${GCLOUD_ACCOUNT:?No active gcloud account found. Run 'gcloud auth login' first.}"

echo "Using gcloud account: ${GCLOUD_ACCOUNT}"
echo "Using gcloud project: ${GCLOUD_PROJECT}"
echo "Resolved tracker version: ${TRACKER_VERSION}"

run_gcloud() {
  CLOUDSDK_CORE_DISABLE_PROMPTS=1 gcloud --quiet "$@"
}

PROJECT_NUMBER="$(run_gcloud projects describe "${GCLOUD_PROJECT}" --format='value(projectNumber)')"
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
ENV_VARS_FILE="$(mktemp)"
trap 'rm -f "${ENV_VARS_FILE}"' EXIT

echo "Using Cloud Run runtime service account: ${RUNTIME_SERVICE_ACCOUNT}"

echo "Checking Secret Manager secret ${SECRET_KEY_SECRET_NAME}..."
if run_gcloud secrets describe "${SECRET_KEY_SECRET_NAME}" --project "${GCLOUD_PROJECT}" >/dev/null 2>&1; then
  echo "Reusing existing Secret Manager secret ${SECRET_KEY_SECRET_NAME}."
else
  BOOTSTRAP_SECRET_KEY="${SECRET_KEY:-$(python3 -c 'import os; print(os.urandom(32).hex())')}"
  echo "Creating Secret Manager secret ${SECRET_KEY_SECRET_NAME}."
  run_gcloud services enable secretmanager.googleapis.com --project "${GCLOUD_PROJECT}"
  run_gcloud secrets create "${SECRET_KEY_SECRET_NAME}" --project "${GCLOUD_PROJECT}" --replication-policy="automatic"
  echo "Adding initial secret version to ${SECRET_KEY_SECRET_NAME}."
  printf %s "${BOOTSTRAP_SECRET_KEY}" | run_gcloud secrets versions add "${SECRET_KEY_SECRET_NAME}" --project "${GCLOUD_PROJECT}" --data-file=-
fi

echo "Granting Secret Manager access on ${SECRET_KEY_SECRET_NAME} to ${RUNTIME_SERVICE_ACCOUNT}."
run_gcloud secrets add-iam-policy-binding "${SECRET_KEY_SECRET_NAME}" \
  --project "${GCLOUD_PROJECT}" \
  --member "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role "roles/secretmanager.secretAccessor"

python3 - <<'PY' > "${ENV_VARS_FILE}"
import json
import os

print(json.dumps({
    "DATABASE_URL": os.environ["DATABASE_URL"],
    "TRACKER_ADMIN_PASS": os.environ["TRACKER_ADMIN_PASS"],
    "TRACKER_API_KEY": os.environ["TRACKER_API_KEY"],
    "TRACKER_VERSION": os.environ["TRACKER_VERSION"],
    "TRACKER_RECENT_VERSIONS": os.environ["TRACKER_RECENT_VERSIONS"],
}))
PY

echo "Wrote Cloud Run env vars file to ${ENV_VARS_FILE}."
echo "Deploying ${SERVICE_NAME} version ${TRACKER_VERSION} to Cloud Run in ${REGION}..."

run_gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --project "${GCLOUD_PROJECT}" \
  --service-account "${RUNTIME_SERVICE_ACCOUNT}" \
  --env-vars-file "${ENV_VARS_FILE}" \
  --set-secrets "SECRET_KEY=${SECRET_KEY_SECRET_NAME}:latest"
