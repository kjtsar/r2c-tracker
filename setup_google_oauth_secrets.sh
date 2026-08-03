#!/bin/sh
set -eu

usage() {
  echo "Usage: $0 PATH_TO_GOOGLE_WEB_CLIENT_JSON" >&2
}

if [ "$#" -ne 1 ]; then
  usage
  exit 2
fi

CLIENT_FILE="$1"
CONFIG_NAME="${R2C_GCLOUD_CONFIG_NAME:-r2c-tracker-pilot}"
PROJECT="${R2C_GCLOUD_PROJECT:-$(
  gcloud --configuration="${CONFIG_NAME}" config get-value project 2>/dev/null
)}"
CLIENT_ID_SECRET="r2c-google-oauth-client-id"
CLIENT_SECRET_SECRET="r2c-google-oauth-client-secret"
PLATFORM_REDIRECT_URI="https://r2c-tracker.com/platform-admin/google/callback"
ORGANIZATION_REDIRECT_URI="https://r2c-tracker.com/google/callback"

if [ ! -f "${CLIENT_FILE}" ]; then
  echo "OAuth client JSON not found: ${CLIENT_FILE}" >&2
  exit 1
fi
if [ -z "${PROJECT}" ] || [ "${PROJECT}" = "(unset)" ]; then
  echo "No GCI project is selected." >&2
  exit 1
fi

oauth_client_id="$(
  python3 - "${CLIENT_FILE}" "${PLATFORM_REDIRECT_URI}" \
    "${ORGANIZATION_REDIRECT_URI}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    web = json.load(source).get("web", {})
client_id = str(web.get("client_id", "")).strip()
client_secret = str(web.get("client_secret", "")).strip()
redirect_uris = web.get("redirect_uris", [])
if not client_id or not client_secret:
    raise SystemExit("The file is not a Google Web application OAuth client.")
for required_uri in sys.argv[2:]:
    if required_uri not in redirect_uris:
        raise SystemExit(
            "The OAuth client does not contain the required redirect URI: "
            + required_uri
        )
print(client_id)
PY
)"
oauth_client_secret="$(
  python3 - "${CLIENT_FILE}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    print(json.load(source)["web"]["client_secret"])
PY
)"

ensure_secret() {
  secret_name="$1"
  if ! gcloud --configuration="${CONFIG_NAME}" secrets describe "${secret_name}" \
    --project="${PROJECT}" >/dev/null 2>&1; then
    gcloud --configuration="${CONFIG_NAME}" secrets create "${secret_name}" \
      --project="${PROJECT}" \
      --replication-policy=automatic
  fi
}

ensure_secret "${CLIENT_ID_SECRET}"
ensure_secret "${CLIENT_SECRET_SECRET}"
printf %s "${oauth_client_id}" |
  gcloud --configuration="${CONFIG_NAME}" secrets versions add "${CLIENT_ID_SECRET}" \
    --project="${PROJECT}" \
    --data-file=-
printf %s "${oauth_client_secret}" |
  gcloud --configuration="${CONFIG_NAME}" secrets versions add "${CLIENT_SECRET_SECRET}" \
    --project="${PROJECT}" \
    --data-file=-

unset oauth_client_id oauth_client_secret
echo "Google OAuth client values stored in Secret Manager for ${PROJECT}."
echo "The source JSON still contains a client secret; protect or remove it."
