#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONFIG_NAME="${R2C_GCLOUD_CONFIG_NAME:-r2c-tracker-pilot}"
ACCOUNT="${R2C_GCLOUD_ACCOUNT:-kjtsar@kjt.us}"
PROJECT="${R2C_GCLOUD_PROJECT:-r2c-tracker-pilot}"
REGION="${R2C_GCLOUD_REGION:-us-west1}"
ENV_FILE="${R2C_PILOT_ENV_FILE:-${SCRIPT_DIR}/.env.pilot.local}"
DATABASE_VM_PROJECT="${R2C_DATABASE_VM_PROJECT:-shaped-splicer-482602-v1}"
DATABASE_VM_ZONE="${R2C_DATABASE_VM_ZONE:-us-west1-b}"
DATABASE_VM_NAME="${R2C_DATABASE_VM_NAME:-instance-20260104-171736}"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is required." >&2
  exit 1
fi

if ! gcloud config configurations describe "${CONFIG_NAME}" >/dev/null 2>&1; then
  gcloud config configurations create "${CONFIG_NAME}" --no-activate
fi

pilot_gcloud() {
  gcloud --configuration="${CONFIG_NAME}" "$@"
}

if ! gcloud auth list --filter="account=${ACCOUNT}" --format='value(account)' | grep -qx "${ACCOUNT}"; then
  echo "The account ${ACCOUNT} is not authenticated. Run:" >&2
  echo "  gcloud auth login ${ACCOUNT}" >&2
  exit 1
fi

pilot_gcloud config set account "${ACCOUNT}"
pilot_gcloud config set project "${PROJECT}"
pilot_gcloud config set run/region "${REGION}"
if ! pilot_gcloud auth print-access-token >/dev/null 2>&1; then
  echo "The credentials for ${ACCOUNT} need to be refreshed. Run:" >&2
  echo "  gcloud auth login ${ACCOUNT}" >&2
  exit 1
fi

actual_project="$(pilot_gcloud config get-value project 2>/dev/null)"
actual_region="$(pilot_gcloud config get-value run/region 2>/dev/null)"
if [ "${actual_project}" != "${PROJECT}" ] || [ "${actual_region}" != "${REGION}" ]; then
  echo "Pilot gcloud configuration did not resolve to the expected project and region." >&2
  exit 1
fi

pilot_gcloud projects describe "${PROJECT}" >/dev/null
pilot_gcloud compute instances describe "${DATABASE_VM_NAME}" \
  --project="${DATABASE_VM_PROJECT}" \
  --zone="${DATABASE_VM_ZONE}" >/dev/null
pilot_gcloud storage buckets describe gs://r2c-tracker-pilot-flightlogs --project="${PROJECT}" >/dev/null

secret_has_enabled_version() {
  [ -n "$(pilot_gcloud secrets versions list "$1" \
    --project="${PROJECT}" \
    --filter='state=ENABLED' \
    --format='value(name)' \
    --limit=1)" ]
}

tracker_admin_pass="$(pilot_gcloud secrets versions access latest --secret=r2c-tracker-admin-password --project="${PROJECT}")"
tracker_api_key="$(pilot_gcloud secrets versions access latest --secret=r2c-tracker-api-key --project="${PROJECT}")"
secret_key="$(pilot_gcloud secrets versions access latest --secret=r2c-tracker-secret-key --project="${PROJECT}")"
control_plane_signing_key=""
if secret_has_enabled_version r2c-control-plane-signing-key; then
  control_plane_signing_key="$(pilot_gcloud secrets versions access latest --secret=r2c-control-plane-signing-key --project="${PROJECT}")"
fi
faa_notam_client_id=""
faa_notam_client_secret=""
if secret_has_enabled_version r2c-faa-notam-client-id; then
  faa_notam_client_id="$(pilot_gcloud secrets versions access latest --secret=r2c-faa-notam-client-id --project="${PROJECT}")"
fi
if secret_has_enabled_version r2c-faa-notam-client-secret; then
  faa_notam_client_secret="$(pilot_gcloud secrets versions access latest --secret=r2c-faa-notam-client-secret --project="${PROJECT}")"
fi

local_database_url="sqlite+aiosqlite:///./test.db"
local_control_plane_database_url="sqlite+aiosqlite:///./control_plane.test.db"

umask 077
{
  printf 'DATABASE_URL=%s\n' "${local_database_url}"
  printf 'TRACKER_ADMIN_PASS=%s\n' "${tracker_admin_pass}"
  printf 'TRACKER_API_KEY=%s\n' "${tracker_api_key}"
  printf 'SECRET_KEY=%s\n' "${secret_key}"
  if [ -n "${local_control_plane_database_url}" ]; then
    printf 'CONTROL_PLANE_DATABASE_URL=%s\n' "${local_control_plane_database_url}"
    printf 'CONTROL_PLANE_MODE=simulation\n'
    printf 'CONTROL_PLANE_PUBLIC_URL=https://r2c-tracker.com\n'
    printf 'CONTROL_PLANE_TRACKER_BASE_URL=https://r2c-tracker.com\n'
    printf 'DEVICE_CREDENTIAL_ISSUANCE_ENABLED=true\n'
    printf 'SESSION_COOKIE_HTTPS_ONLY=false\n'
  fi
  if [ -n "${control_plane_signing_key}" ]; then
    printf 'CONTROL_PLANE_SIGNING_KEY=%s\n' "${control_plane_signing_key}"
  fi
  if [ -n "${local_control_plane_database_url}" ]; then
    printf 'PLATFORM_BILLING_SOURCE=bigquery\n'
    printf 'PLATFORM_BILLING_PROJECT=r2c-tracker-platform\n'
    printf 'PLATFORM_BILLING_DATASET=r2c_billing_export\n'
    printf 'PLATFORM_BILLING_INCLUDED_PROJECTS=shaped-splicer-482602-v1,r2c-tracker-pilot,r2c-tracker-platform\n'
  fi
  if [ -n "${faa_notam_client_id}" ]; then
    printf 'FAA_NOTAM_CLIENT_ID=%s\n' "${faa_notam_client_id}"
  fi
  if [ -n "${faa_notam_client_secret}" ]; then
    printf 'FAA_NOTAM_CLIENT_SECRET=%s\n' "${faa_notam_client_secret}"
  fi
  printf 'TRACKER_PORT=8080\n'
  printf 'GCLOUD_PROJECT=%s\n' "${PROJECT}"
  printf 'REGION=%s\n' "${REGION}"
} > "${ENV_FILE}"

chmod 600 "${ENV_FILE}"

echo "Pilot gcloud configuration ready: ${CONFIG_NAME}"
echo "Project: ${PROJECT}"
echo "Region: ${REGION}"
echo "Secret-backed local environment written to ${ENV_FILE}"
echo "Local development uses separate SQLite tracker and control-plane files."
echo "Load ${ENV_FILE} and start the tracker locally; the deployed pilot databases remain private."
