#!/bin/sh
set -eu

CONFIG_NAME="${R2C_GCLOUD_CONFIG_NAME:-r2c-tracker-pilot}"
PILOT_PROJECT="${R2C_GCLOUD_PROJECT:-r2c-tracker-pilot}"
PLATFORM_PROJECT="${R2C_PLATFORM_PROJECT:-r2c-tracker-platform}"
RUNTIME_SERVICE_ACCOUNT="${R2C_RUNTIME_SERVICE_ACCOUNT:-r2c-tracker-runtime@r2c-tracker-pilot.iam.gserviceaccount.com}"
TRACKER_DATABASE_SECRET="${DATABASE_URL_SECRET_NAME:-r2c-tracker-database-url}"
DATABASE_SECRET="${CONTROL_PLANE_DATABASE_URL_SECRET_NAME:-r2c-control-plane-database-url}"
SIGNING_SECRET="${CONTROL_PLANE_SIGNING_KEY_SECRET_NAME:-r2c-control-plane-signing-key}"

pilot_gcloud() {
  gcloud --configuration="${CONFIG_NAME}" --quiet "$@"
}

if [ "$(pilot_gcloud config get-value project 2>/dev/null)" != "${PILOT_PROJECT}" ]; then
  echo "Refusing to prepare the control plane outside ${PILOT_PROJECT}." >&2
  exit 1
fi

secret_has_enabled_version() {
  [ -n "$(pilot_gcloud secrets versions list "$1" \
    --project="${PILOT_PROJECT}" \
    --filter='state=ENABLED' \
    --format='value(name)' \
    --limit=1 2>/dev/null || true)" ]
}

for database_secret in "${TRACKER_DATABASE_SECRET}" "${DATABASE_SECRET}"; do
  if ! secret_has_enabled_version "${database_secret}"; then
    echo "Database secret ${database_secret} must already contain the isolated VM database URL." >&2
    exit 1
  fi
done

ensure_secret_value() {
  secret_name="$1"
  secret_value="$2"
  if ! pilot_gcloud secrets describe "${secret_name}" \
    --project="${PILOT_PROJECT}" >/dev/null 2>&1; then
    pilot_gcloud secrets create "${secret_name}" \
      --project="${PILOT_PROJECT}" \
      --replication-policy=automatic
  fi
  if ! secret_has_enabled_version "${secret_name}"; then
    printf %s "${secret_value}" | pilot_gcloud secrets versions add "${secret_name}" \
      --project="${PILOT_PROJECT}" \
      --data-file=-
  fi
  echo "Secret ${secret_name} is ready."
}

signing_key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
ensure_secret_value "${SIGNING_SECRET}" "${signing_key}"
unset signing_key

echo "Granting the runtime service account read-only access to aggregate billing exports..."
pilot_gcloud projects add-iam-policy-binding "${PLATFORM_PROJECT}" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role=roles/bigquery.jobUser >/dev/null
pilot_gcloud projects add-iam-policy-binding "${PLATFORM_PROJECT}" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role=roles/bigquery.dataViewer >/dev/null

echo "Pilot control-plane resources are ready."
echo "Run ./set_super_admin.sh EMAIL 'DISPLAY NAME' if the infrastructure identity is not configured."
echo "Run ./setup_pilot_local.sh to refresh the local secret-backed environment."
