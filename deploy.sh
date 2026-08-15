#!/bin/sh
set -eu

usage() {
  echo "Usage: $0 R2C_RECOMMENDED_APP_VERSION_CODE [R2C_UPDATE_URL]" >&2
  echo "Example: $0 77" >&2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  usage
  exit 2
fi

case "$1" in
  ""|*[!0-9]*)
    usage
    echo "R2C_RECOMMENDED_APP_VERSION_CODE must be a positive integer." >&2
    exit 2
    ;;
esac

if [ "$1" -le 0 ]; then
  usage
  echo "R2C_RECOMMENDED_APP_VERSION_CODE must be a positive integer." >&2
  exit 2
fi

R2C_RECOMMENDED_APP_VERSION_CODE="$1"
R2C_UPDATE_URL="${2:-}"
export R2C_RECOMMENDED_APP_VERSION_CODE
export R2C_UPDATE_URL
CONTAINER_IMAGE="${CONTAINER_IMAGE:-}"
export CONTAINER_IMAGE
DEPLOY_SOURCE_DIR="${DEPLOY_SOURCE_DIR:-.}"
export DEPLOY_SOURCE_DIR
if [ -n "${CONTAINER_IMAGE}" ]; then
  python3 -c 'import os, re, sys; value=os.environ["CONTAINER_IMAGE"]; sys.exit(0 if re.fullmatch(r"[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}", value) else 1)' || {
    echo "CONTAINER_IMAGE must be an immutable Artifact Registry sha256 digest." >&2
    exit 2
  }
fi
if [ -z "${CONTAINER_IMAGE}" ] && [ ! -d "${DEPLOY_SOURCE_DIR}" ]; then
  echo "DEPLOY_SOURCE_DIR does not exist or is not a directory: ${DEPLOY_SOURCE_DIR}" >&2
  exit 2
fi
R2C_RECOMMENDED_IOS_APP_BUILD_NUMBER="${R2C_RECOMMENDED_IOS_APP_BUILD_NUMBER:-0}"
R2C_IOS_UPDATE_URL="${R2C_IOS_UPDATE_URL:-}"
export R2C_RECOMMENDED_IOS_APP_BUILD_NUMBER
export R2C_IOS_UPDATE_URL

DATABASE_URL_SECRET_NAME="${DATABASE_URL_SECRET_NAME:-}"
TRACKER_ADMIN_PASS_SECRET_NAME="${TRACKER_ADMIN_PASS_SECRET_NAME:-}"
DEPLOYMENT_GATE_KEY_SECRET_NAME="${DEPLOYMENT_GATE_KEY_SECRET_NAME:-}"
CONTROL_PLANE_DATABASE_URL_SECRET_NAME="${CONTROL_PLANE_DATABASE_URL_SECRET_NAME:-}"
CONTROL_PLANE_SIGNING_KEY_SECRET_NAME="${CONTROL_PLANE_SIGNING_KEY_SECRET_NAME:-}"
MANAGED_REQUEST_INGEST_KEY_SECRET_NAME="${MANAGED_REQUEST_INGEST_KEY_SECRET_NAME:-}"
GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME="${GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME:-}"
GOOGLE_OAUTH_CLIENT_SECRET_SECRET_NAME="${GOOGLE_OAUTH_CLIENT_SECRET_SECRET_NAME:-}"
MICROSOFT_OIDC_CLIENT_ID_SECRET_NAME="${MICROSOFT_OIDC_CLIENT_ID_SECRET_NAME:-}"
MICROSOFT_OIDC_CLIENT_SECRET_SECRET_NAME="${MICROSOFT_OIDC_CLIENT_SECRET_SECRET_NAME:-}"
PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME="${PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME:-}"
PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME="${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME:-}"
PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET="${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET:-}"
CLOUDFLARE_TURN_KEY_ID_SECRET_NAME="${CLOUDFLARE_TURN_KEY_ID_SECRET_NAME:-}"
CLOUDFLARE_TURN_API_TOKEN_SECRET_NAME="${CLOUDFLARE_TURN_API_TOKEN_SECRET_NAME:-}"
export DATABASE_URL_SECRET_NAME
export TRACKER_ADMIN_PASS_SECRET_NAME
export DEPLOYMENT_GATE_KEY_SECRET_NAME
export CONTROL_PLANE_DATABASE_URL_SECRET_NAME
export CONTROL_PLANE_SIGNING_KEY_SECRET_NAME
export MANAGED_REQUEST_INGEST_KEY_SECRET_NAME
export GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME
export GOOGLE_OAUTH_CLIENT_SECRET_SECRET_NAME
export MICROSOFT_OIDC_CLIENT_ID_SECRET_NAME
export MICROSOFT_OIDC_CLIENT_SECRET_SECRET_NAME
export PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME
export PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME
export PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET
export CLOUDFLARE_TURN_KEY_ID_SECRET_NAME
export CLOUDFLARE_TURN_API_TOKEN_SECRET_NAME

if [ -z "${DATABASE_URL:-}" ] && [ -z "${DATABASE_URL_SECRET_NAME}" ]; then
  echo "DATABASE_URL or DATABASE_URL_SECRET_NAME must be set." >&2
  exit 1
fi
if [ -n "${MICROSOFT_OIDC_CLIENT_ID_SECRET_NAME}" ] \
  || [ -n "${MICROSOFT_OIDC_CLIENT_SECRET_SECRET_NAME}" ]; then
  if [ -z "${MICROSOFT_OIDC_CLIENT_ID_SECRET_NAME}" ] \
    || [ -z "${MICROSOFT_OIDC_CLIENT_SECRET_SECRET_NAME}" ]; then
    echo "Both Microsoft OIDC Secret Manager names must be configured together." >&2
    exit 1
  fi
fi
if [ -z "${TRACKER_ADMIN_PASS:-}" ] && [ -z "${TRACKER_ADMIN_PASS_SECRET_NAME}" ]; then
  echo "TRACKER_ADMIN_PASS or TRACKER_ADMIN_PASS_SECRET_NAME must be set." >&2
  exit 1
fi
if [ -z "${DEPLOYMENT_GATE_KEY:-}" ] && [ -z "${DEPLOYMENT_GATE_KEY_SECRET_NAME}" ]; then
  echo "DEPLOYMENT_GATE_KEY or DEPLOYMENT_GATE_KEY_SECRET_NAME must be set." >&2
  exit 1
fi
if [ -n "${GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME}" ] \
  || [ -n "${GOOGLE_OAUTH_CLIENT_SECRET_SECRET_NAME}" ]; then
  if [ -z "${GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME}" ] \
    || [ -z "${GOOGLE_OAUTH_CLIENT_SECRET_SECRET_NAME}" ]; then
    echo "Both Google OAuth Secret Manager names must be configured together." >&2
    exit 1
  fi
fi
if [ -n "${CLOUDFLARE_TURN_KEY_ID_SECRET_NAME}" ] \
  || [ -n "${CLOUDFLARE_TURN_API_TOKEN_SECRET_NAME}" ]; then
  if [ -z "${CLOUDFLARE_TURN_KEY_ID_SECRET_NAME}" ] \
    || [ -z "${CLOUDFLARE_TURN_API_TOKEN_SECRET_NAME}" ]; then
    echo "Both Cloudflare TURN Secret Manager names must be configured together." >&2
    exit 1
  fi
fi

validate_database_url() {
  python3 - <<'PY'
import os
import sys
from urllib.parse import parse_qs, urlsplit

url = os.environ["DATABASE_URL"]
try:
    parts = urlsplit(url)
except ValueError as exc:
    sys.stderr.write(f"DATABASE_URL is not parseable: {exc}\n")
    sys.stderr.write(
        "This usually means the password contains reserved URL characters like @, :, /, ?, #, [, ], or % "
        "and needs to be percent-encoded.\n"
    )
    sys.exit(1)

query = parse_qs(parts.query)
socket_hosts = query.get("host", [])
has_cloud_sql_socket = any(host.startswith("/cloudsql/") for host in socket_hosts)

errors = []
if not parts.scheme:
    errors.append("missing URL scheme")
if not parts.hostname and not has_cloud_sql_socket:
    errors.append("missing hostname")
if parts.hostname and parts.port is None:
    errors.append("missing numeric port")
if not parts.path or parts.path == "/":
    errors.append("missing database name in path")

if errors:
    sys.stderr.write("DATABASE_URL does not look valid: " + ", ".join(errors) + ".\n")
    sys.stderr.write(
        "If your DB password contains reserved URL characters like @, :, /, ?, #, [, ], or %, "
        "it must be percent-encoded inside DATABASE_URL.\n"
    )
    sys.exit(1)
PY
}

if [ -n "${DATABASE_URL:-}" ]; then
  validate_database_url
fi

REGION="${REGION:-us-west1}"
SERVICE_NAME="${SERVICE_NAME:-r2c-tracker}"
SECRET_KEY_SECRET_NAME="${SECRET_KEY_SECRET_NAME:-r2c-tracker-secret-key}"
FAA_CLIENT_ID_SECRET_NAME="${FAA_CLIENT_ID_SECRET_NAME:-r2c-faa-notam-client-id}"
FAA_CLIENT_SECRET_SECRET_NAME="${FAA_CLIENT_SECRET_SECRET_NAME:-r2c-faa-notam-client-secret}"
PLATFORM_ADMIN_IDENTITY_SECRET_NAME="r2c-super-admin-identity"
CLOUD_SQL_INSTANCE="${CLOUD_SQL_INSTANCE:-}"
CLOUD_RUN_NETWORK="${CLOUD_RUN_NETWORK:-}"
CLOUD_RUN_SUBNET="${CLOUD_RUN_SUBNET:-}"
CLOUD_RUN_VPC_EGRESS="${CLOUD_RUN_VPC_EGRESS:-private-ranges-only}"
FLIGHTLOGS_BUCKET="${FLIGHTLOGS_BUCKET:-}"
export FLIGHTLOGS_BUCKET
FLIGHTLOGS_VOLUME_NAME="${FLIGHTLOGS_VOLUME_NAME:-flightlogs}"
CLOUD_RUN_MEMORY="${CLOUD_RUN_MEMORY:-}"
ALLOW_UNAUTHENTICATED="${ALLOW_UNAUTHENTICATED:-0}"
TRACKER_VERSION="${TRACKER_VERSION:-$(git describe --tags --always 2>/dev/null || echo unknown)}"
if [ -z "${TRACKER_RECENT_VERSIONS:-}" ]; then
TRACKER_RECENT_VERSIONS="$(python3 - <<'PY'
import json
import subprocess
from pathlib import Path

def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()

try:
    documented = {}
    active_tag = None
    for raw_line in Path("changes.txt").read_text().splitlines():
        clean_line = raw_line.strip()
        if clean_line.startswith("v") and clean_line.endswith(": deployed"):
            active_tag = clean_line.removesuffix(": deployed")
            documented[active_tag] = []
            continue
        if clean_line.startswith("v") and clean_line.endswith(": pending review"):
            active_tag = clean_line.removesuffix(": pending review")
            documented[active_tag] = []
            continue
        if raw_line.startswith("v") and raw_line.rstrip().endswith(":"):
            active_tag = clean_line.removesuffix(":")
            documented[active_tag] = []
            continue
        if active_tag and raw_line.lstrip().startswith("*"):
            documented[active_tag].append(raw_line.strip())
    tags = run(["git", "tag", "--sort=-creatordate"]).splitlines()
    versions = []
    for tag in [tag.strip() for tag in tags if tag.strip()][:10]:
        versions.append({
            "tag": tag,
            "date": run(["git", "log", "-1", "--date=short", "--format=%ad", tag]),
            "summary": " ".join(documented.get(tag, [])) or run(
                ["git", "log", "-1", "--format=%s", tag]
            ),
        })
    print(json.dumps(versions))
except Exception:
    print("[]")
PY
)"
fi
GCLOUD_PROJECT="${GCLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
GCLOUD_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n 1 || true)"

export TRACKER_VERSION
export TRACKER_RECENT_VERSIONS

: "${GCLOUD_PROJECT:?No active gcloud project found. Run 'gcloud config set project YOUR_PROJECT_ID' or export GCLOUD_PROJECT.}"
: "${GCLOUD_ACCOUNT:?No active gcloud account found. Run 'gcloud auth login' first.}"

echo "Using gcloud account: ${GCLOUD_ACCOUNT}"
echo "Using gcloud project: ${GCLOUD_PROJECT}"
echo "Resolved tracker version: ${TRACKER_VERSION}"
echo "Recommended RID2Caltopo versionCode: ${R2C_RECOMMENDED_APP_VERSION_CODE}"
echo "Recommended RID2Caltopo iOS build: ${R2C_RECOMMENDED_IOS_APP_BUILD_NUMBER}"
if [ -n "${R2C_UPDATE_URL}" ]; then
  echo "RID2Caltopo update URL configured."
else
  echo "RID2Caltopo update URL not configured."
fi

run_gcloud() {
  CLOUDSDK_CORE_DISABLE_PROMPTS=1 gcloud --quiet "$@"
}

secret_has_enabled_version() {
  secret_name="$1"
  [ -n "$(run_gcloud secrets versions list "${secret_name}" \
    --project "${GCLOUD_PROJECT}" \
    --filter='state=ENABLED' \
    --format='value(name)' \
    --limit=1)" ]
}

require_existing_secret() {
  secret_name="$1"
  env_name="$2"
  echo "Checking Secret Manager secret ${secret_name} for ${env_name}..."
  if ! run_gcloud secrets describe "${secret_name}" --project "${GCLOUD_PROJECT}" >/dev/null 2>&1; then
    echo "Required Secret Manager secret ${secret_name} does not exist." >&2
    exit 1
  fi
  if ! secret_has_enabled_version "${secret_name}"; then
    echo "Required Secret Manager secret ${secret_name} has no enabled version." >&2
    exit 1
  fi
  run_gcloud secrets add-iam-policy-binding "${secret_name}" \
    --project "${GCLOUD_PROJECT}" \
    --member "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
    --role "roles/secretmanager.secretAccessor"
}

PROJECT_NUMBER="$(run_gcloud projects describe "${GCLOUD_PROJECT}" --format='value(projectNumber)')"
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
ENV_VARS_FILE="$(mktemp)"
trap 'rm -f "${ENV_VARS_FILE}"' EXIT

echo "Using Cloud Run runtime service account: ${RUNTIME_SERVICE_ACCOUNT}"

FAST_UI_DEPLOY="${FAST_UI_DEPLOY:-0}"
case "${FAST_UI_DEPLOY}" in
  0|1) ;;
  *) echo "FAST_UI_DEPLOY must be 0 or 1." >&2; exit 2 ;;
esac

if [ "${FAST_UI_DEPLOY}" = "1" ]; then
  echo "Fast UI deployment: reusing the production service's existing secrets and IAM bindings."
else
if [ -n "${DATABASE_URL_SECRET_NAME}" ]; then
  require_existing_secret "${DATABASE_URL_SECRET_NAME}" "DATABASE_URL"
fi
if [ -n "${TRACKER_ADMIN_PASS_SECRET_NAME}" ]; then
  require_existing_secret "${TRACKER_ADMIN_PASS_SECRET_NAME}" "TRACKER_ADMIN_PASS"
fi
if [ -n "${DEPLOYMENT_GATE_KEY_SECRET_NAME}" ]; then
  require_existing_secret "${DEPLOYMENT_GATE_KEY_SECRET_NAME}" "DEPLOYMENT_GATE_KEY"
fi
if [ -n "${CONTROL_PLANE_DATABASE_URL_SECRET_NAME}" ]; then
  require_existing_secret "${CONTROL_PLANE_DATABASE_URL_SECRET_NAME}" "CONTROL_PLANE_DATABASE_URL"
fi
if [ -n "${GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME}" ]; then
  require_existing_secret "${GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME}" \
    "GOOGLE_OAUTH_CLIENT_ID"
  require_existing_secret "${GOOGLE_OAUTH_CLIENT_SECRET_SECRET_NAME}" \
    "GOOGLE_OAUTH_CLIENT_SECRET"
fi
if [ -n "${MICROSOFT_OIDC_CLIENT_ID_SECRET_NAME}" ]; then
  require_existing_secret "${MICROSOFT_OIDC_CLIENT_ID_SECRET_NAME}" \
    "MICROSOFT_OIDC_CLIENT_ID"
  require_existing_secret "${MICROSOFT_OIDC_CLIENT_SECRET_SECRET_NAME}" \
    "MICROSOFT_OIDC_CLIENT_SECRET"
fi
if [ -n "${PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME}" ]; then
  require_existing_secret "${PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME}" \
    "PLATFORM_EMAIL_SMTP_PASSWORD"
fi
if [ -n "${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME}" ]; then
  require_existing_secret "${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME}" \
    "PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN"
fi
if [ -n "${CLOUDFLARE_TURN_KEY_ID_SECRET_NAME}" ]; then
  require_existing_secret "${CLOUDFLARE_TURN_KEY_ID_SECRET_NAME}" \
    "CLOUDFLARE_TURN_KEY_ID"
  require_existing_secret "${CLOUDFLARE_TURN_API_TOKEN_SECRET_NAME}" \
    "CLOUDFLARE_TURN_API_TOKEN"
fi
if [ -n "${CONTROL_PLANE_SIGNING_KEY_SECRET_NAME}" ]; then
  require_existing_secret "${CONTROL_PLANE_SIGNING_KEY_SECRET_NAME}" "CONTROL_PLANE_SIGNING_KEY"
fi
if [ -n "${MANAGED_REQUEST_INGEST_KEY_SECRET_NAME}" ]; then
  require_existing_secret "${MANAGED_REQUEST_INGEST_KEY_SECRET_NAME}" "MANAGED_REQUEST_INGEST_KEY"
fi
if [ -n "${CONTROL_PLANE_DATABASE_URL_SECRET_NAME}" ]; then
  require_existing_secret "${PLATFORM_ADMIN_IDENTITY_SECRET_NAME}" \
    "the dynamic platform administrator identity"
fi
if [ -n "${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET}" ]; then
  EXPECTED_SECRET_PREFIX="projects/${GCLOUD_PROJECT}/secrets/"
  case "${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET}" in
    "${EXPECTED_SECRET_PREFIX}"*) ;;
    *)
      echo "PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET must name a secret in ${GCLOUD_PROJECT}." >&2
      exit 1
      ;;
  esac
  GMAIL_TARGET_SECRET_NAME="${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET#${EXPECTED_SECRET_PREFIX}}"
  case "${GMAIL_TARGET_SECRET_NAME}" in
    ""|*/*)
      echo "PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET is not a Secret Manager parent resource." >&2
      exit 1
      ;;
  esac
  run_gcloud services enable gmail.googleapis.com secretmanager.googleapis.com \
    --project "${GCLOUD_PROJECT}"
  if ! run_gcloud secrets describe "${GMAIL_TARGET_SECRET_NAME}" \
      --project "${GCLOUD_PROJECT}" >/dev/null 2>&1; then
    run_gcloud secrets create "${GMAIL_TARGET_SECRET_NAME}" \
      --project "${GCLOUD_PROJECT}" --replication-policy="automatic"
  fi
  run_gcloud secrets add-iam-policy-binding "${GMAIL_TARGET_SECRET_NAME}" \
    --project "${GCLOUD_PROJECT}" \
    --member "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
    --role "roles/secretmanager.secretVersionAdder"
fi

echo "Checking Secret Manager secret ${SECRET_KEY_SECRET_NAME}..."
if run_gcloud secrets describe "${SECRET_KEY_SECRET_NAME}" --project "${GCLOUD_PROJECT}" >/dev/null 2>&1; then
  echo "Reusing existing Secret Manager secret ${SECRET_KEY_SECRET_NAME}."
  if ! secret_has_enabled_version "${SECRET_KEY_SECRET_NAME}"; then
    BOOTSTRAP_SECRET_KEY="${SECRET_KEY:-$(python3 -c 'import os; print(os.urandom(32).hex())')}"
    echo "Adding initial secret version to ${SECRET_KEY_SECRET_NAME}."
    printf %s "${BOOTSTRAP_SECRET_KEY}" | run_gcloud secrets versions add "${SECRET_KEY_SECRET_NAME}" --project "${GCLOUD_PROJECT}" --data-file=-
  fi
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

echo "Checking Secret Manager secret ${FAA_CLIENT_ID_SECRET_NAME}..."
if run_gcloud secrets describe "${FAA_CLIENT_ID_SECRET_NAME}" --project "${GCLOUD_PROJECT}" >/dev/null 2>&1; then
  echo "Reusing existing Secret Manager secret ${FAA_CLIENT_ID_SECRET_NAME}."
  if ! secret_has_enabled_version "${FAA_CLIENT_ID_SECRET_NAME}"; then
    if [ -z "${FAA_NOTAM_CLIENT_ID:-}" ]; then
      echo "${FAA_CLIENT_ID_SECRET_NAME} has no enabled version. Add one in Secret Manager or set FAA_NOTAM_CLIENT_ID." >&2
      exit 1
    fi
    printf %s "${FAA_NOTAM_CLIENT_ID}" | run_gcloud secrets versions add "${FAA_CLIENT_ID_SECRET_NAME}" --project "${GCLOUD_PROJECT}" --data-file=-
  fi
else
  if [ -z "${FAA_NOTAM_CLIENT_ID:-}" ]; then
    echo "FAA_NOTAM_CLIENT_ID must be set when creating ${FAA_CLIENT_ID_SECRET_NAME}." >&2
    exit 1
  fi
  run_gcloud services enable secretmanager.googleapis.com --project "${GCLOUD_PROJECT}"
  run_gcloud secrets create "${FAA_CLIENT_ID_SECRET_NAME}" --project "${GCLOUD_PROJECT}" --replication-policy="automatic"
  printf %s "${FAA_NOTAM_CLIENT_ID}" | run_gcloud secrets versions add "${FAA_CLIENT_ID_SECRET_NAME}" --project "${GCLOUD_PROJECT}" --data-file=-
fi
run_gcloud secrets add-iam-policy-binding "${FAA_CLIENT_ID_SECRET_NAME}" \
  --project "${GCLOUD_PROJECT}" \
  --member "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role "roles/secretmanager.secretAccessor"

echo "Checking Secret Manager secret ${FAA_CLIENT_SECRET_SECRET_NAME}..."
if run_gcloud secrets describe "${FAA_CLIENT_SECRET_SECRET_NAME}" --project "${GCLOUD_PROJECT}" >/dev/null 2>&1; then
  echo "Reusing existing Secret Manager secret ${FAA_CLIENT_SECRET_SECRET_NAME}."
  if ! secret_has_enabled_version "${FAA_CLIENT_SECRET_SECRET_NAME}"; then
    if [ -z "${FAA_NOTAM_CLIENT_SECRET:-}" ]; then
      echo "${FAA_CLIENT_SECRET_SECRET_NAME} has no enabled version. Add one in Secret Manager or set FAA_NOTAM_CLIENT_SECRET." >&2
      exit 1
    fi
    printf %s "${FAA_NOTAM_CLIENT_SECRET}" | run_gcloud secrets versions add "${FAA_CLIENT_SECRET_SECRET_NAME}" --project "${GCLOUD_PROJECT}" --data-file=-
  fi
else
  if [ -z "${FAA_NOTAM_CLIENT_SECRET:-}" ]; then
    echo "FAA_NOTAM_CLIENT_SECRET must be set when creating ${FAA_CLIENT_SECRET_SECRET_NAME}." >&2
    exit 1
  fi
  run_gcloud services enable secretmanager.googleapis.com --project "${GCLOUD_PROJECT}"
  run_gcloud secrets create "${FAA_CLIENT_SECRET_SECRET_NAME}" --project "${GCLOUD_PROJECT}" --replication-policy="automatic"
  printf %s "${FAA_NOTAM_CLIENT_SECRET}" | run_gcloud secrets versions add "${FAA_CLIENT_SECRET_SECRET_NAME}" --project "${GCLOUD_PROJECT}" --data-file=-
fi
run_gcloud secrets add-iam-policy-binding "${FAA_CLIENT_SECRET_SECRET_NAME}" \
  --project "${GCLOUD_PROJECT}" \
  --member "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role "roles/secretmanager.secretAccessor"
fi

python3 - <<'PY' > "${ENV_VARS_FILE}"
import json
import os

print(json.dumps({
    "TRACKER_VERSION": os.environ["TRACKER_VERSION"],
    "TRACKER_RECENT_VERSIONS": os.environ["TRACKER_RECENT_VERSIONS"],
    "R2C_RECOMMENDED_APP_VERSION_CODE": os.environ["R2C_RECOMMENDED_APP_VERSION_CODE"],
    "R2C_UPDATE_URL": os.environ["R2C_UPDATE_URL"],
    "R2C_RECOMMENDED_IOS_APP_BUILD_NUMBER": os.environ["R2C_RECOMMENDED_IOS_APP_BUILD_NUMBER"],
    "R2C_IOS_UPDATE_URL": os.environ["R2C_IOS_UPDATE_URL"],
    "FAA_NOTAM_API_BASE_URL": os.environ.get("FAA_NOTAM_API_BASE_URL", "https://api-nms.aim.faa.gov/nmsapi"),
    "FAA_NOTAM_TOKEN_URL": os.environ.get("FAA_NOTAM_TOKEN_URL", "https://api-nms.aim.faa.gov/v1/auth/token"),
    "FAA_PROXY_CACHE_TTL_SEC": os.environ.get("FAA_PROXY_CACHE_TTL_SEC", "90"),
    "FAA_PROXY_CACHE_MAX_ENTRIES": os.environ.get("FAA_PROXY_CACHE_MAX_ENTRIES", "512"),
    "FAA_PROXY_CACHE_MAX_BYTES": os.environ.get("FAA_PROXY_CACHE_MAX_BYTES", "67108864"),
    "FAA_PROXY_CACHE_MAX_ITEM_BYTES": os.environ.get("FAA_PROXY_CACHE_MAX_ITEM_BYTES", "8388608"),
    "FAA_PROXY_CACHE_GRID_DEGREES": os.environ.get("FAA_PROXY_CACHE_GRID_DEGREES", "0.002"),
    "FAA_PROXY_MAX_CONCURRENT_UPSTREAM": os.environ.get("FAA_PROXY_MAX_CONCURRENT_UPSTREAM", "8"),
    "PLATFORM_BILLING_SOURCE": os.environ.get("PLATFORM_BILLING_SOURCE", "illustrative"),
    "PLATFORM_BILLING_PROJECT": os.environ.get("PLATFORM_BILLING_PROJECT", ""),
    "PLATFORM_BILLING_DATASET": os.environ.get("PLATFORM_BILLING_DATASET", ""),
    "PLATFORM_BILLING_INCLUDED_PROJECTS": os.environ.get("PLATFORM_BILLING_INCLUDED_PROJECTS", ""),
    "CONTROL_PLANE_MODE": os.environ.get("CONTROL_PLANE_MODE", "simulation"),
    "RELEASE_STAGING_MODE": os.environ.get("RELEASE_STAGING_MODE", "false"),
    "CONTROL_PLANE_PUBLIC_URL": os.environ.get("CONTROL_PLANE_PUBLIC_URL", ""),
    "CONTROL_PLANE_TRACKER_BASE_URL": os.environ.get("CONTROL_PLANE_TRACKER_BASE_URL", ""),
    "DEVICE_CREDENTIAL_ISSUANCE_ENABLED": os.environ.get("DEVICE_CREDENTIAL_ISSUANCE_ENABLED", "false"),
    "SESSION_COOKIE_HTTPS_ONLY": os.environ.get("SESSION_COOKIE_HTTPS_ONLY", "false"),
    "VIDEO_ICE_SERVERS_JSON": os.environ.get("VIDEO_ICE_SERVERS_JSON", "[]"),
    "FLIGHTLOGS_STORAGE_REQUIRED": "true" if os.environ.get("FLIGHTLOGS_BUCKET") else "false",
    "CLOUDFLARE_TURN_CREDENTIAL_TTL_SECONDS": os.environ.get(
        "CLOUDFLARE_TURN_CREDENTIAL_TTL_SECONDS", "3600"
    ),
    "PLATFORM_EMAIL_SMTP_HOST": os.environ.get("PLATFORM_EMAIL_SMTP_HOST", ""),
    "PLATFORM_EMAIL_SMTP_PORT": os.environ.get("PLATFORM_EMAIL_SMTP_PORT", "587"),
    "PLATFORM_EMAIL_SMTP_USER": os.environ.get("PLATFORM_EMAIL_SMTP_USER", ""),
    "PLATFORM_EMAIL_FROM": os.environ.get("PLATFORM_EMAIL_FROM", ""),
    "PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET": os.environ.get(
        "PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET", ""
    ),
    "MICROSOFT_OIDC_TENANT": os.environ.get(
        "MICROSOFT_OIDC_TENANT", "organizations"
    ),
    **({
        "DATABASE_URL": os.environ["DATABASE_URL"]
    } if os.environ.get("DATABASE_URL") and not os.environ.get("DATABASE_URL_SECRET_NAME") else {}),
    **({
        "TRACKER_ADMIN_PASS": os.environ["TRACKER_ADMIN_PASS"]
    } if os.environ.get("TRACKER_ADMIN_PASS") and not os.environ.get("TRACKER_ADMIN_PASS_SECRET_NAME") else {}),
    **({
        "DEPLOYMENT_GATE_KEY": os.environ["DEPLOYMENT_GATE_KEY"]
    } if os.environ.get("DEPLOYMENT_GATE_KEY") and not os.environ.get("DEPLOYMENT_GATE_KEY_SECRET_NAME") else {}),
    **({
        "CONTROL_PLANE_DATABASE_URL": os.environ["CONTROL_PLANE_DATABASE_URL"]
    } if os.environ.get("CONTROL_PLANE_DATABASE_URL") and not os.environ.get("CONTROL_PLANE_DATABASE_URL_SECRET_NAME") else {}),
    **({
        "CONTROL_PLANE_SIGNING_KEY": os.environ["CONTROL_PLANE_SIGNING_KEY"]
    } if os.environ.get("CONTROL_PLANE_SIGNING_KEY") and not os.environ.get("CONTROL_PLANE_SIGNING_KEY_SECRET_NAME") else {}),
}))
PY

echo "Wrote Cloud Run env vars file to ${ENV_VARS_FILE}."
echo "Deploying ${SERVICE_NAME} version ${TRACKER_VERSION} to Cloud Run in ${REGION}..."

WEB_REQUEST_TIMEOUT="${WEB_REQUEST_TIMEOUT:-3600s}"
ACTIVATE_LATEST_REVISION="${ACTIVATE_LATEST_REVISION:-1}"

SECRET_MAPPINGS="SECRET_KEY=${SECRET_KEY_SECRET_NAME}:latest,FAA_NOTAM_CLIENT_ID=${FAA_CLIENT_ID_SECRET_NAME}:latest,FAA_NOTAM_CLIENT_SECRET=${FAA_CLIENT_SECRET_SECRET_NAME}:latest"
if [ -n "${DATABASE_URL_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},DATABASE_URL=${DATABASE_URL_SECRET_NAME}:latest"
fi
if [ -n "${TRACKER_ADMIN_PASS_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},TRACKER_ADMIN_PASS=${TRACKER_ADMIN_PASS_SECRET_NAME}:latest"
fi
if [ -n "${DEPLOYMENT_GATE_KEY_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},DEPLOYMENT_GATE_KEY=${DEPLOYMENT_GATE_KEY_SECRET_NAME}:latest"
fi
if [ -n "${CONTROL_PLANE_DATABASE_URL_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},CONTROL_PLANE_DATABASE_URL=${CONTROL_PLANE_DATABASE_URL_SECRET_NAME}:latest"
fi
if [ -n "${CONTROL_PLANE_SIGNING_KEY_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},CONTROL_PLANE_SIGNING_KEY=${CONTROL_PLANE_SIGNING_KEY_SECRET_NAME}:latest"
fi
if [ -n "${MANAGED_REQUEST_INGEST_KEY_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},MANAGED_REQUEST_INGEST_KEY=${MANAGED_REQUEST_INGEST_KEY_SECRET_NAME}:latest"
fi
if [ -n "${GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME}:latest"
  SECRET_MAPPINGS="${SECRET_MAPPINGS},GOOGLE_OAUTH_CLIENT_SECRET=${GOOGLE_OAUTH_CLIENT_SECRET_SECRET_NAME}:latest"
fi
if [ -n "${MICROSOFT_OIDC_CLIENT_ID_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},MICROSOFT_OIDC_CLIENT_ID=${MICROSOFT_OIDC_CLIENT_ID_SECRET_NAME}:latest"
  SECRET_MAPPINGS="${SECRET_MAPPINGS},MICROSOFT_OIDC_CLIENT_SECRET=${MICROSOFT_OIDC_CLIENT_SECRET_SECRET_NAME}:latest"
fi
if [ -n "${PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},PLATFORM_EMAIL_SMTP_PASSWORD=${PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME}:latest"
fi
if [ -n "${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN=${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME}:latest"
fi
if [ -n "${CLOUDFLARE_TURN_KEY_ID_SECRET_NAME}" ]; then
  SECRET_MAPPINGS="${SECRET_MAPPINGS},CLOUDFLARE_TURN_KEY_ID=${CLOUDFLARE_TURN_KEY_ID_SECRET_NAME}:latest"
  SECRET_MAPPINGS="${SECRET_MAPPINGS},CLOUDFLARE_TURN_API_TOKEN=${CLOUDFLARE_TURN_API_TOKEN_SECRET_NAME}:latest"
fi
set -- run deploy "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${GCLOUD_PROJECT}" \
  --service-account "${RUNTIME_SERVICE_ACCOUNT}" \
  --timeout "${WEB_REQUEST_TIMEOUT}" \
  --max-instances 1 \
  --startup-probe "httpGet.path=/livez,timeoutSeconds=5,periodSeconds=5,failureThreshold=24" \
  --liveness-probe "httpGet.path=/livez,initialDelaySeconds=10,timeoutSeconds=5,periodSeconds=30,failureThreshold=3" \
  --env-vars-file "${ENV_VARS_FILE}" \
  --set-secrets "${SECRET_MAPPINGS}"

if [ -n "${CONTAINER_IMAGE}" ]; then
  set -- "$@" --image "${CONTAINER_IMAGE}"
else
  set -- "$@" --source "${DEPLOY_SOURCE_DIR}"
fi

if [ -n "${CLOUD_RUN_MEMORY}" ]; then
  set -- "$@" --memory "${CLOUD_RUN_MEMORY}"
fi
if [ "${ALLOW_UNAUTHENTICATED}" = "1" ]; then
  set -- "$@" --allow-unauthenticated
else
  set -- "$@" --no-allow-unauthenticated
fi
if [ -n "${CLOUD_SQL_INSTANCE}" ]; then
  set -- "$@" --set-cloudsql-instances "${CLOUD_SQL_INSTANCE}"
else
  set -- "$@" --clear-cloudsql-instances
fi
if [ -n "${CLOUD_RUN_NETWORK}" ] || [ -n "${CLOUD_RUN_SUBNET}" ]; then
  if [ -z "${CLOUD_RUN_NETWORK}" ] || [ -z "${CLOUD_RUN_SUBNET}" ]; then
    echo "CLOUD_RUN_NETWORK and CLOUD_RUN_SUBNET must be configured together." >&2
    exit 1
  fi
  set -- "$@" \
    --network "${CLOUD_RUN_NETWORK}" \
    --subnet "${CLOUD_RUN_SUBNET}" \
    --vpc-egress "${CLOUD_RUN_VPC_EGRESS}"
fi
if [ -n "${FLIGHTLOGS_BUCKET}" ]; then
  set -- "$@" \
    --execution-environment gen2 \
    --add-volume "name=${FLIGHTLOGS_VOLUME_NAME},type=cloud-storage,bucket=${FLIGHTLOGS_BUCKET}" \
    --add-volume-mount "volume=${FLIGHTLOGS_VOLUME_NAME},mount-path=/flightlogs-vol"
fi
if [ "${ACTIVATE_LATEST_REVISION}" != "1" ]; then
  set -- "$@" --no-traffic
fi
if [ -n "${REVISION_TAG:-}" ]; then
  set -- "$@" --tag "${REVISION_TAG}"
fi

run_gcloud "$@"

if [ "${ACTIVATE_LATEST_REVISION}" = "1" ]; then
  echo "Routing 100% of ${SERVICE_NAME} traffic to the latest ready revision..."
  run_gcloud run services update-traffic "${SERVICE_NAME}" \
    --region "${REGION}" \
    --project "${GCLOUD_PROJECT}" \
    --to-latest
else
  echo "Leaving Cloud Run traffic unchanged because ACTIVATE_LATEST_REVISION=${ACTIVATE_LATEST_REVISION}."
fi
