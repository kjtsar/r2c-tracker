#!/bin/sh
set -eu

HOURS="${1:-24}"

case "$HOURS" in
  ''|*[!0-9]*)
    echo "Usage: $0 [hours]" >&2
    echo "hours must be a non-negative integer" >&2
    exit 1
    ;;
esac

END_TS="$(python3 -c 'from datetime import datetime, UTC; print(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
START_TS="$(python3 -c 'from datetime import datetime, UTC, timedelta; import sys; hours=int(sys.argv[1]); print((datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$HOURS")"

gcloud logging read "
resource.type=\"cloud_run_revision\"
resource.labels.service_name=\"r2c-tracker\"
logName:\"run.googleapis.com%2Frequests\"
timestamp>=\"$START_TS\"
timestamp<\"$END_TS\"
" --limit=200 --format=json >allServices.json

gcloud logging read "
resource.type=\"cloud_run_revision\"
resource.labels.service_name=\"r2c-tracker\"
logName:\"run.googleapis.com%2Frequests\"
httpRequest.requestUrl:(\"/r2c\" OR \"/ws\")
timestamp>=\"$START_TS\"
timestamp<\"$END_TS\"
" --limit=200 --format=json >r2cOrWsServices.json

