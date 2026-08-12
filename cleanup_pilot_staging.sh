#!/bin/sh
set -eu

CONFIG="${CLOUDSDK_ACTIVE_CONFIG_NAME:-r2c-tracker-pilot}"
PROJECT="${GCLOUD_PROJECT:-r2c-tracker-pilot}"
REGION="${REGION:-us-west1}"
STAGING_SERVICE="r2c-tracker-staging"
STAGING_INSTANCE="r2c-release-staging"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
STAGING_STATE_PATH="${SCRIPT_DIR}/.release-state/staging.json"

if [ "${PROJECT}" != "r2c-tracker-pilot" ]; then
  echo "Refusing staging cleanup in unexpected project ${PROJECT}." >&2
  exit 1
fi

pilot_gcloud() {
  gcloud --configuration="${CONFIG}" --quiet "$@"
}

if pilot_gcloud run services describe "${STAGING_SERVICE}" \
    --project "${PROJECT}" --region "${REGION}" >/dev/null 2>&1; then
  pilot_gcloud run services delete "${STAGING_SERVICE}" \
    --project "${PROJECT}" --region "${REGION}"
fi
if pilot_gcloud sql instances describe "${STAGING_INSTANCE}" \
    --project "${PROJECT}" >/dev/null 2>&1; then
  pilot_gcloud sql instances delete "${STAGING_INSTANCE}" \
    --project "${PROJECT}"
fi
rm -f "${STAGING_STATE_PATH}"

echo "Staging Cloud Run service and isolated Cloud SQL instance removed."
echo "Staging secrets, service account, and empty bucket remain reusable."
