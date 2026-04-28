#!/bin/sh

set -eu

SERVICE_NAME="${SERVICE_NAME:-r2c-tracker}"
LIMIT="${LIMIT:-200}"
FRESHNESS="${FRESHNESS:-}"

BASE_FILTER=$(cat <<EOF
resource.type="cloud_run_revision"
resource.labels.service_name="${SERVICE_NAME}"
EOF
)

if [ -n "${FRESHNESS}" ]; then
  TIME_FILTER=$(cat <<EOF
${BASE_FILTER}
timestamp>="${FRESHNESS}"
EOF
)
else
  TIME_FILTER="${BASE_FILTER}"
fi

REQUEST_FILTER=$(cat <<EOF
${TIME_FILTER}
httpRequest.requestUrl:"/ws/r2c"
EOF
)

APP_FILTER=$(cat <<EOF
${TIME_FILTER}
(
  logName="projects/shaped-splicer-482602-v1/logs/run.googleapis.com%2Fstdout" OR
  logName="projects/shaped-splicer-482602-v1/logs/run.googleapis.com%2Fstderr"
)
(
  textPayload:"r2c " OR
  textPayload:"heartbeat_ack" OR
  textPayload:"owner_assigned" OR
  textPayload:"owner_expired" OR
  jsonPayload.message:"r2c " OR
  jsonPayload.message:"heartbeat_ack" OR
  jsonPayload.message:"owner_assigned" OR
  jsonPayload.message:"owner_expired"
)
EOF
)

echo "=== WS Request Logs ==="
gcloud logging read "${REQUEST_FILTER}" --limit="${LIMIT}" --format=json
echo
echo "=== App Logs ==="
gcloud logging read "${APP_FILTER}" --limit="${LIMIT}" --format=json
