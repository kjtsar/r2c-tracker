#!/bin/sh
set -eu

usage() {
  echo "Usage: $0 EMAIL [DISPLAY_NAME]" >&2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  usage
  exit 2
fi

EMAIL="$1"
DISPLAY_NAME="${2:-R2C Platform Administrator}"
CONFIG_NAME="${R2C_GCLOUD_CONFIG_NAME:-r2c-tracker-pilot}"
PROJECT="${R2C_GCLOUD_PROJECT:-$(
  gcloud --configuration="${CONFIG_NAME}" config get-value project 2>/dev/null
)}"
SECRET_NAME="r2c-super-admin-identity"

if ! python3 - "${EMAIL}" "${DISPLAY_NAME}" <<'PY'
import re
import sys

email = sys.argv[1].strip().lower()
display_name = sys.argv[2].strip()
if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
    raise SystemExit("Enter a valid super-admin email address.")
if not display_name:
    raise SystemExit("Display name cannot be empty.")
PY
then
  exit 2
fi

if [ -z "${PROJECT}" ] || [ "${PROJECT}" = "(unset)" ]; then
  echo "No GCI project is selected." >&2
  exit 1
fi

if ! gcloud --configuration="${CONFIG_NAME}" secrets describe "${SECRET_NAME}" \
  --project="${PROJECT}" >/dev/null 2>&1; then
  gcloud --configuration="${CONFIG_NAME}" services enable secretmanager.googleapis.com \
    --project="${PROJECT}"
  gcloud --configuration="${CONFIG_NAME}" secrets create "${SECRET_NAME}" \
    --project="${PROJECT}" \
    --replication-policy=automatic
fi

python3 - "${EMAIL}" "${DISPLAY_NAME}" <<'PY' |
import json
import sys

print(json.dumps({
    "email": sys.argv[1].strip().lower(),
    "display_name": sys.argv[2].strip(),
}, separators=(",", ":")))
PY
  gcloud --configuration="${CONFIG_NAME}" secrets versions add "${SECRET_NAME}" \
    --project="${PROJECT}" \
    --data-file=-

echo "Super-admin identity updated in ${PROJECT}."
echo "Active sessions for the former identity will expire within 30 seconds."
