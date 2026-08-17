#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

export CLOUDSDK_ACTIVE_CONFIG_NAME="${CLOUDSDK_ACTIVE_CONFIG_NAME:-r2c-tracker-pilot}"
export GCLOUD_PROJECT="${GCLOUD_PROJECT:-r2c-tracker-pilot}"
export REGION="${REGION:-us-west1}"
export SERVICE_NAME="${SERVICE_NAME:-r2c-tracker-staging}"
export RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT:-r2c-tracker-staging@r2c-tracker-pilot.iam.gserviceaccount.com}"
export DATABASE_URL_SECRET_NAME="${DATABASE_URL_SECRET_NAME:-r2c-staging-tracker-database-url}"
export CONTROL_PLANE_DATABASE_URL_SECRET_NAME="${CONTROL_PLANE_DATABASE_URL_SECRET_NAME:-r2c-staging-control-plane-database-url}"
export TRACKER_ADMIN_PASS_SECRET_NAME="${TRACKER_ADMIN_PASS_SECRET_NAME:-r2c-staging-tracker-admin-password}"
export DEPLOYMENT_GATE_KEY_SECRET_NAME="${DEPLOYMENT_GATE_KEY_SECRET_NAME:-r2c-staging-deployment-gate-key}"
export SECRET_KEY_SECRET_NAME="${SECRET_KEY_SECRET_NAME:-r2c-staging-secret-key}"
export CONTROL_PLANE_SIGNING_KEY_SECRET_NAME="${CONTROL_PLANE_SIGNING_KEY_SECRET_NAME:-r2c-staging-control-plane-signing-key}"
export MANAGED_REQUEST_INGEST_KEY_SECRET_NAME=""
export GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME=""
export GOOGLE_OAUTH_CLIENT_SECRET_SECRET_NAME=""
export MICROSOFT_OIDC_CLIENT_ID_SECRET_NAME=""
export MICROSOFT_OIDC_CLIENT_SECRET_SECRET_NAME=""
export MICROSOFT_OIDC_TENANT="organizations"
export PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME=""
export PLATFORM_EMAIL_SMTP_HOST=""
export PLATFORM_EMAIL_SMTP_USER=""
export PLATFORM_EMAIL_FROM=""
export PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME=""
export PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET=""
export APP_STORE_CONNECT_WEBHOOK_SECRET_NAME=""
export TESTFLIGHT_FEEDBACK_EMAIL=""
export CLOUDFLARE_TURN_KEY_ID_SECRET_NAME=""
export CLOUDFLARE_TURN_API_TOKEN_SECRET_NAME=""
export CLOUD_SQL_INSTANCE="${CLOUD_SQL_INSTANCE:-r2c-tracker-pilot:us-west1:r2c-release-staging}"
export CLOUD_RUN_NETWORK="${CLOUD_RUN_NETWORK:-r2c-pilot-vpc}"
export CLOUD_RUN_SUBNET="${CLOUD_RUN_SUBNET:-r2c-pilot-us-west1}"
export CLOUD_RUN_VPC_EGRESS="${CLOUD_RUN_VPC_EGRESS:-private-ranges-only}"
export FLIGHTLOGS_BUCKET="${FLIGHTLOGS_BUCKET:-r2c-tracker-staging-flightlogs}"
export CLOUD_RUN_MEMORY="${CLOUD_RUN_MEMORY:-1Gi}"
export ALLOW_UNAUTHENTICATED="0"
export CONTROL_PLANE_MODE="simulation"
export RELEASE_STAGING_MODE="true"
export CONTROL_PLANE_PUBLIC_URL="https://staging.invalid"
export CONTROL_PLANE_TRACKER_BASE_URL="https://staging.invalid"
export DEVICE_CREDENTIAL_ISSUANCE_ENABLED="true"
export SESSION_COOKIE_HTTPS_ONLY="true"
export PLATFORM_BILLING_SOURCE="illustrative"
export VIDEO_ICE_SERVERS_JSON='[]'
export FAA_NOTAM_API_BASE_URL="https://api-staging.cgifederal-aim.com/nmsapi"
export FAA_NOTAM_TOKEN_URL="https://api-staging.cgifederal-aim.com/v1/auth/token"

if [ "${GCLOUD_PROJECT}" != "r2c-tracker-pilot" ]; then
  echo "Refusing staging deployment to unexpected project ${GCLOUD_PROJECT}." >&2
  exit 1
fi
if [ "${SERVICE_NAME}" != "r2c-tracker-staging" ]; then
  echo "Refusing staging deployment to unexpected service ${SERVICE_NAME}." >&2
  exit 1
fi

exec "${SCRIPT_DIR}/deploy.sh" "$@"
