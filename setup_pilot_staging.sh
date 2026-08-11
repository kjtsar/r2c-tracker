#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="${PYTHON:-${SCRIPT_DIR}/.venv/bin/python}"
CONFIG="${CLOUDSDK_ACTIVE_CONFIG_NAME:-r2c-tracker-pilot}"
PROJECT="${GCLOUD_PROJECT:-r2c-tracker-pilot}"
REGION="${REGION:-us-west1}"
STAGING_INSTANCE="r2c-release-staging"
STAGING_SERVICE="r2c-tracker-staging"
STAGING_SERVICE_ACCOUNT="r2c-tracker-staging@${PROJECT}.iam.gserviceaccount.com"
STAGING_BUCKET="r2c-tracker-staging-flightlogs"
TRACKER_ROLE="r2c_stage_tracker_user"
CONTROL_ROLE="r2c_stage_control_user"
TRACKER_DATABASE="r2c_stage_tracker"
CONTROL_DATABASE="r2c_stage_control_plane"

if [ "${PROJECT}" != "r2c-tracker-pilot" ]; then
  echo "Refusing staging setup in unexpected project ${PROJECT}." >&2
  exit 1
fi

pilot_gcloud() {
  gcloud --configuration="${CONFIG}" --quiet "$@"
}

for command in gcloud "${PYTHON}"; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "Required command is unavailable: ${command}" >&2
    exit 1
  fi
done

if pilot_gcloud run services describe "${STAGING_SERVICE}" \
    --project "${PROJECT}" --region "${REGION}" >/dev/null 2>&1; then
  echo "Staging service already exists; run ./cleanup_pilot_staging.sh first." >&2
  exit 1
fi
if pilot_gcloud sql instances describe "${STAGING_INSTANCE}" \
    --project "${PROJECT}" >/dev/null 2>&1; then
  echo "Staging Cloud SQL instance already exists; run ./cleanup_pilot_staging.sh first." >&2
  exit 1
fi

tracker_password="$(${PYTHON} -c 'import secrets; print(secrets.token_urlsafe(36))')"
control_password="$(${PYTHON} -c 'import secrets; print(secrets.token_urlsafe(36))')"
root_password="$(${PYTHON} -c 'import secrets; print(secrets.token_urlsafe(36))')"
temporary_dir="$(mktemp -d)"
instance_created="0"
setup_complete="0"
cleanup() {
  rm -rf "${temporary_dir}"
  if [ "${instance_created}" = "1" ] && [ "${setup_complete}" != "1" ]; then
    pilot_gcloud sql instances delete "${STAGING_INSTANCE}" \
      --project "${PROJECT}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT HUP INT TERM
export R2C_STAGE_ROOT_PASSWORD="${root_password}"
export R2C_STAGE_TRACKER_PASSWORD="${tracker_password}"
export R2C_STAGE_CONTROL_PASSWORD="${control_password}"
export R2C_STAGE_FLAGS_DIR="${temporary_dir}"
${PYTHON} - <<'PY'
import os
from pathlib import Path

root = Path(os.environ["R2C_STAGE_FLAGS_DIR"])
for filename, key, value in (
    ("root.yaml", "--root-password", os.environ["R2C_STAGE_ROOT_PASSWORD"]),
    ("tracker.yaml", "--password", os.environ["R2C_STAGE_TRACKER_PASSWORD"]),
    ("control.yaml", "--password", os.environ["R2C_STAGE_CONTROL_PASSWORD"]),
):
    path = root / filename
    path.write_text(f"{key}: {value}\n")
    path.chmod(0o600)
PY

pilot_gcloud sql instances create "${STAGING_INSTANCE}" \
  --project "${PROJECT}" \
  --database-version POSTGRES_15 \
  --tier db-f1-micro \
  --region "${REGION}" \
  --availability-type zonal \
  --storage-type SSD \
  --storage-size 10 \
  --no-storage-auto-increase \
  --no-deletion-protection \
  --flags-file "${temporary_dir}/root.yaml"
instance_created="1"

pilot_gcloud sql databases create "${TRACKER_DATABASE}" \
  --instance "${STAGING_INSTANCE}" --project "${PROJECT}"
pilot_gcloud sql databases create "${CONTROL_DATABASE}" \
  --instance "${STAGING_INSTANCE}" --project "${PROJECT}"
pilot_gcloud sql users create "${TRACKER_ROLE}" \
  --instance "${STAGING_INSTANCE}" --project "${PROJECT}" \
  --flags-file "${temporary_dir}/tracker.yaml"
pilot_gcloud sql users create "${CONTROL_ROLE}" \
  --instance "${STAGING_INSTANCE}" --project "${PROJECT}" \
  --flags-file "${temporary_dir}/control.yaml"

create_secret() {
  secret_name="$1"
  secret_value="$2"
  if pilot_gcloud secrets describe "${secret_name}" --project "${PROJECT}" >/dev/null 2>&1; then
    printf %s "${secret_value}" | pilot_gcloud secrets versions add "${secret_name}" \
      --project "${PROJECT}" --data-file=- >/dev/null
  else
    pilot_gcloud secrets create "${secret_name}" --project "${PROJECT}" \
      --replication-policy=automatic >/dev/null
    printf %s "${secret_value}" | pilot_gcloud secrets versions add "${secret_name}" \
      --project "${PROJECT}" --data-file=- >/dev/null
  fi
}

connection_name="${PROJECT}:${REGION}:${STAGING_INSTANCE}"
create_secret "r2c-staging-tracker-database-url" \
  "postgresql+asyncpg://${TRACKER_ROLE}:${tracker_password}@/${TRACKER_DATABASE}?host=/cloudsql/${connection_name}"
create_secret "r2c-staging-control-plane-database-url" \
  "postgresql+asyncpg://${CONTROL_ROLE}:${control_password}@/${CONTROL_DATABASE}?host=/cloudsql/${connection_name}"
create_secret "r2c-staging-tracker-admin-password" "$(${PYTHON} -c 'import secrets; print(secrets.token_urlsafe(36))')"
create_secret "r2c-staging-deployment-gate-key" "$(${PYTHON} -c 'import secrets; print(secrets.token_urlsafe(48))')"
create_secret "r2c-staging-secret-key" "$(${PYTHON} -c 'import secrets; print(secrets.token_urlsafe(48))')"
create_secret "r2c-staging-control-plane-signing-key" "$(${PYTHON} -c 'import secrets; print(secrets.token_urlsafe(48))')"

if ! pilot_gcloud iam service-accounts describe "${STAGING_SERVICE_ACCOUNT}" \
    --project "${PROJECT}" >/dev/null 2>&1; then
  pilot_gcloud iam service-accounts create r2c-tracker-staging \
    --project "${PROJECT}" --display-name "R2C Tracker staging runtime"
fi
pilot_gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:${STAGING_SERVICE_ACCOUNT}" \
  --role roles/cloudsql.client >/dev/null

if ! pilot_gcloud storage buckets describe "gs://${STAGING_BUCKET}" \
    --project "${PROJECT}" >/dev/null 2>&1; then
  pilot_gcloud storage buckets create "gs://${STAGING_BUCKET}" \
    --project "${PROJECT}" --location "${REGION}" --uniform-bucket-level-access
fi
pilot_gcloud storage buckets add-iam-policy-binding "gs://${STAGING_BUCKET}" \
  --member "serviceAccount:${STAGING_SERVICE_ACCOUNT}" \
  --role roles/storage.objectAdmin >/dev/null

for secret_name in \
  r2c-staging-tracker-database-url \
  r2c-staging-control-plane-database-url \
  r2c-staging-tracker-admin-password \
  r2c-staging-deployment-gate-key \
  r2c-staging-secret-key \
  r2c-staging-control-plane-signing-key; do
  pilot_gcloud secrets add-iam-policy-binding "${secret_name}" \
    --project "${PROJECT}" \
    --member "serviceAccount:${STAGING_SERVICE_ACCOUNT}" \
    --role roles/secretmanager.secretAccessor >/dev/null
done

setup_complete="1"
unset tracker_password control_password root_password
unset R2C_STAGE_ROOT_PASSWORD R2C_STAGE_TRACKER_PASSWORD R2C_STAGE_CONTROL_PASSWORD
echo "Ephemeral Cloud SQL staging instance, secrets, service account, and bucket are ready."
echo "Run ./scripts/refresh_staging_databases.sh before deploying a release candidate."
