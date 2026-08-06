#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 SMTP_HOST SMTP_USER FROM_ADDRESS" >&2
  echo "The SMTP password is prompted without echo and stored in Secret Manager." >&2
  exit 2
fi

SMTP_HOST="$1"
SMTP_USER="$2"
FROM_ADDRESS="$3"
CONFIG_NAME="${R2C_GCLOUD_CONFIG_NAME:-r2c-tracker-pilot}"
PILOT_PROJECT="${R2C_GCLOUD_PROJECT:-r2c-tracker-pilot}"
RUNTIME_SERVICE_ACCOUNT="${R2C_RUNTIME_SERVICE_ACCOUNT:-r2c-tracker-runtime@r2c-tracker-pilot.iam.gserviceaccount.com}"
SMTP_SECRET="${PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME:-r2c-platform-email-smtp-password}"

pilot_gcloud() {
  gcloud --configuration="${CONFIG_NAME}" --quiet "$@"
}

if [ "$(pilot_gcloud config get-value project 2>/dev/null)" != "${PILOT_PROJECT}" ]; then
  echo "Refusing to configure email outside ${PILOT_PROJECT}." >&2
  exit 1
fi

smtp_password="$(python3 -c 'import getpass; print(getpass.getpass("SMTP password or provider API key: "))')"
if [ -z "${smtp_password}" ]; then
  echo "SMTP password cannot be empty." >&2
  exit 1
fi

if ! pilot_gcloud secrets describe "${SMTP_SECRET}" \
  --project="${PILOT_PROJECT}" >/dev/null 2>&1; then
  pilot_gcloud secrets create "${SMTP_SECRET}" \
    --project="${PILOT_PROJECT}" \
    --replication-policy=automatic
fi
printf %s "${smtp_password}" | pilot_gcloud secrets versions add "${SMTP_SECRET}" \
  --project="${PILOT_PROJECT}" \
  --data-file=- >/dev/null
unset smtp_password

pilot_gcloud secrets add-iam-policy-binding "${SMTP_SECRET}" \
  --project="${PILOT_PROJECT}" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role=roles/secretmanager.secretAccessor >/dev/null

echo "Pilot SMTP secret is ready."
echo "Deploy live provisioning with:"
echo "PLATFORM_EMAIL_SMTP_HOST='${SMTP_HOST}' PLATFORM_EMAIL_SMTP_USER='${SMTP_USER}' PLATFORM_EMAIL_FROM='${FROM_ADDRESS}' PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME='${SMTP_SECRET}' CONTROL_PLANE_MODE=live ./deploy_pilot.sh APP_VERSION_CODE"
