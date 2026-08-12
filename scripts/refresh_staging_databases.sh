#!/bin/sh
set -eu

CONFIG="${CLOUDSDK_ACTIVE_CONFIG_NAME:-r2c-tracker-pilot}"
PROJECT="${GCLOUD_PROJECT:-r2c-tracker-pilot}"
REGION="${REGION:-us-west1}"
DB_PROJECT="${R2C_DATABASE_VM_PROJECT:-shaped-splicer-482602-v1}"
DB_ZONE="${R2C_DATABASE_VM_ZONE:-us-west1-b}"
DB_VM="${R2C_DATABASE_VM_NAME:-instance-20260104-171736}"
STAGING_INSTANCE="r2c-release-staging"
STAGING_SERVICE="r2c-tracker-staging"
STAGING_BUCKET="r2c-tracker-staging-flightlogs"
FIREWALL_RULE="allow-iap-postgres-r2c-staging"
PROD_PORT="15433"
STAGE_PORT="15434"
if [ -x /opt/homebrew/opt/postgresql@15/bin/pg_dump ]; then
  PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@15/bin}"
else
  PG_BIN="${PG_BIN:-$(dirname "$(command -v pg_dump 2>/dev/null || echo /missing/pg_dump)")}"
fi
PG_DUMP="${PG_BIN}/pg_dump"
PG_RESTORE="${PG_BIN}/pg_restore"
PG_ISREADY="${PG_BIN}/pg_isready"

if [ "${PROJECT}" != "r2c-tracker-pilot" ]; then
  echo "Refusing staging refresh in unexpected project ${PROJECT}." >&2
  exit 1
fi

pilot_gcloud() {
  gcloud --configuration="${CONFIG}" --quiet "$@"
}

for command in gcloud cloud-sql-proxy "${PG_DUMP}" "${PG_RESTORE}" "${PG_ISREADY}" python3; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "Required command is unavailable: ${command}" >&2
    exit 1
  fi
done
pg_dump_major="$(${PG_DUMP} --version | awk '{split($3, version, "."); print version[1]}')"
if [ "${pg_dump_major}" -ne 15 ]; then
  echo "PostgreSQL 15 client tools are required; found $(${PG_DUMP} --version)." >&2
  exit 1
fi

pilot_gcloud sql instances describe "${STAGING_INSTANCE}" \
  --project "${PROJECT}" >/dev/null

if pilot_gcloud run services describe "${STAGING_SERVICE}" \
    --project "${PROJECT}" --region "${REGION}" >/dev/null 2>&1; then
  pilot_gcloud run services delete "${STAGING_SERVICE}" \
    --project "${PROJECT}" --region "${REGION}"
fi

temporary_dir="$(mktemp -d)"
iap_pid=""
proxy_pid=""
tracker_job_pid=""
control_job_pid=""
created_firewall="0"
cleanup() {
  if [ -n "${tracker_job_pid}" ]; then kill "${tracker_job_pid}" >/dev/null 2>&1 || true; fi
  if [ -n "${control_job_pid}" ]; then kill "${control_job_pid}" >/dev/null 2>&1 || true; fi
  if [ -n "${iap_pid}" ]; then kill "${iap_pid}" >/dev/null 2>&1 || true; fi
  if [ -n "${proxy_pid}" ]; then kill "${proxy_pid}" >/dev/null 2>&1 || true; fi
  if [ "${created_firewall}" = "1" ]; then
    pilot_gcloud compute firewall-rules delete "${FIREWALL_RULE}" \
      --project "${DB_PROJECT}" >/dev/null 2>&1 || true
  fi
  rm -rf "${temporary_dir}"
}
trap cleanup EXIT HUP INT TERM

if pilot_gcloud compute firewall-rules describe "${FIREWALL_RULE}" \
    --project "${DB_PROJECT}" >/dev/null 2>&1; then
  echo "Removing stale temporary staging firewall rule before refresh."
  pilot_gcloud compute firewall-rules delete "${FIREWALL_RULE}" \
    --project "${DB_PROJECT}"
fi
pilot_gcloud compute firewall-rules create "${FIREWALL_RULE}" \
  --project "${DB_PROJECT}" \
  --network default \
  --direction INGRESS \
  --action ALLOW \
  --rules tcp:5433 \
  --source-ranges 35.235.240.0/20 \
  --target-tags r2c-pilot-db \
  --description "Temporary authenticated IAP PostgreSQL access for isolated release staging clone"
created_firewall="1"

pilot_gcloud compute start-iap-tunnel "${DB_VM}" 5433 \
  --project "${DB_PROJECT}" --zone "${DB_ZONE}" \
  --local-host-port "127.0.0.1:${PROD_PORT}" \
  >"${temporary_dir}/iap.log" 2>&1 &
iap_pid="$!"
cloud-sql-proxy "${PROJECT}:${REGION}:${STAGING_INSTANCE}" \
  --address 127.0.0.1 --port "${STAGE_PORT}" --gcloud-auth \
  >"${temporary_dir}/proxy.log" 2>&1 &
proxy_pid="$!"

prod_tracker_url="$(pilot_gcloud secrets versions access latest --project "${PROJECT}" --secret r2c-tracker-database-url)"
prod_control_url="$(pilot_gcloud secrets versions access latest --project "${PROJECT}" --secret r2c-control-plane-database-url)"
stage_tracker_url="$(pilot_gcloud secrets versions access latest --project "${PROJECT}" --secret r2c-staging-tracker-database-url)"
stage_control_url="$(pilot_gcloud secrets versions access latest --project "${PROJECT}" --secret r2c-staging-control-plane-database-url)"
export prod_tracker_url prod_control_url stage_tracker_url stage_control_url
export R2C_STAGE_PGPASS="${temporary_dir}/pgpass"
python3 - <<'PY'
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

entries = []
for env_name, port in (
    ("prod_tracker_url", "15433"),
    ("prod_control_url", "15433"),
    ("stage_tracker_url", "15434"),
    ("stage_control_url", "15434"),
):
    parts = urlsplit(os.environ[env_name].replace("postgresql+asyncpg://", "postgresql://", 1))
    entries.append(
        f"127.0.0.1:{port}:{parts.path.lstrip('/')}:{unquote(parts.username or '')}:{unquote(parts.password or '')}"
    )
path = Path(os.environ["R2C_STAGE_PGPASS"])
path.write_text("\n".join(entries) + "\n")
path.chmod(0o600)
PY
unset prod_tracker_url prod_control_url stage_tracker_url stage_control_url
export PGPASSFILE="${temporary_dir}/pgpass"

ready="0"
attempt="0"
while [ "${attempt}" -lt 30 ]; do
  if "${PG_ISREADY}" -h 127.0.0.1 -p "${PROD_PORT}" -d r2c_pilot_tracker -U r2c_pilot_app >/dev/null 2>&1 \
      && "${PG_ISREADY}" -h 127.0.0.1 -p "${STAGE_PORT}" -d r2c_stage_tracker -U r2c_stage_tracker_user >/dev/null 2>&1; then
    ready="1"
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done
if [ "${ready}" != "1" ]; then
  echo "Database tunnels did not become ready." >&2
  exit 1
fi

echo "Dumping independent production databases in parallel."
"${PG_DUMP}" --format=custom --no-owner --no-acl \
  --host 127.0.0.1 --port "${PROD_PORT}" --username r2c_pilot_app \
  --dbname r2c_pilot_tracker --file "${temporary_dir}/tracker.dump" \
  >"${temporary_dir}/tracker-dump.log" 2>&1 &
tracker_dump_pid="$!"
tracker_job_pid="${tracker_dump_pid}"
"${PG_DUMP}" --format=custom --no-owner --no-acl \
  --host 127.0.0.1 --port "${PROD_PORT}" --username r2c_pilot_app \
  --dbname r2c_pilot_control_plane --file "${temporary_dir}/control.dump" \
  >"${temporary_dir}/control-dump.log" 2>&1 &
control_dump_pid="$!"
control_job_pid="${control_dump_pid}"
set +e
wait "${tracker_dump_pid}"
tracker_dump_status="$?"
wait "${control_dump_pid}"
control_dump_status="$?"
tracker_job_pid=""
control_job_pid=""
set -e
if [ "${tracker_dump_status}" -ne 0 ] || [ "${control_dump_status}" -ne 0 ]; then
  cat "${temporary_dir}/tracker-dump.log" >&2
  cat "${temporary_dir}/control-dump.log" >&2
  echo "Staging database dump failed: tracker=${tracker_dump_status} control=${control_dump_status}." >&2
  exit 1
fi

pilot_gcloud sql databases delete r2c_stage_tracker \
  --instance "${STAGING_INSTANCE}" --project "${PROJECT}"
pilot_gcloud sql databases delete r2c_stage_control_plane \
  --instance "${STAGING_INSTANCE}" --project "${PROJECT}"
pilot_gcloud sql databases create r2c_stage_tracker \
  --instance "${STAGING_INSTANCE}" --project "${PROJECT}"
pilot_gcloud sql databases create r2c_stage_control_plane \
  --instance "${STAGING_INSTANCE}" --project "${PROJECT}"

echo "Restoring independent staging databases in parallel."
"${PG_RESTORE}" --no-owner --no-acl --exit-on-error \
  --host 127.0.0.1 --port "${STAGE_PORT}" --username r2c_stage_tracker_user \
  --dbname r2c_stage_tracker "${temporary_dir}/tracker.dump" \
  >"${temporary_dir}/tracker-restore.log" 2>&1 &
tracker_restore_pid="$!"
tracker_job_pid="${tracker_restore_pid}"
"${PG_RESTORE}" --no-owner --no-acl --exit-on-error \
  --host 127.0.0.1 --port "${STAGE_PORT}" --username r2c_stage_control_user \
  --dbname r2c_stage_control_plane "${temporary_dir}/control.dump" \
  >"${temporary_dir}/control-restore.log" 2>&1 &
control_restore_pid="$!"
control_job_pid="${control_restore_pid}"
set +e
wait "${tracker_restore_pid}"
tracker_restore_status="$?"
wait "${control_restore_pid}"
control_restore_status="$?"
tracker_job_pid=""
control_job_pid=""
set -e
if [ "${tracker_restore_status}" -ne 0 ] || [ "${control_restore_status}" -ne 0 ]; then
  cat "${temporary_dir}/tracker-restore.log" >&2
  cat "${temporary_dir}/control-restore.log" >&2
  echo "Staging database restore failed: tracker=${tracker_restore_status} control=${control_restore_status}." >&2
  exit 1
fi

echo "Staging database clones refreshed in isolated Cloud SQL."
echo "The isolated bucket gs://${STAGING_BUCKET} has no production objects copied."
