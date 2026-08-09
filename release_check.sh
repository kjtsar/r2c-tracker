#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "${ROOT_DIR}"

PYTHON="${PYTHON:-.venv/bin/python}"
HOST="${TRACKER_RELEASE_CHECK_HOST:-127.0.0.1}"
PORT="${TRACKER_RELEASE_CHECK_PORT:-18080}"
API_KEY="${TRACKER_API_KEY:-release-check-token}"
DEPLOYMENT_GATE_KEY="${DEPLOYMENT_GATE_KEY:-release-check-deployment-gate}"
ADMIN_PASS="${TRACKER_ADMIN_PASS:-release-check-admin-pass}"
TMP_ROOT="${TMPDIR:-/tmp}"
TMP_DIR="${TMP_ROOT%/}/r2c-tracker-release-check.$$"
DB_PATH="${TMP_DIR}/test.db"
SERVER_LOG="${TMP_DIR}/uvicorn.log"
SERVER_PID=""

cleanup() {
  if [ -n "${SERVER_PID}" ]; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
    wait "${SERVER_PID}" >/dev/null 2>&1 || true
  fi
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT INT TERM

if [ ! -x "${PYTHON}" ]; then
  echo "Python runtime not found at ${PYTHON}." >&2
  echo "Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 2
fi

mkdir -p "${TMP_DIR}"

export DATABASE_URL="sqlite+aiosqlite:///${DB_PATH}"
export TRACKER_API_KEY="${API_KEY}"
export DEPLOYMENT_GATE_KEY
export TRACKER_ADMIN_USER="${TRACKER_ADMIN_USER:-admin}"
export TRACKER_ADMIN_PASS="${ADMIN_PASS}"
export SECRET_KEY="${SECRET_KEY:-release-check-secret}"
export TRACKER_PORT="${PORT}"
export PORT="${PORT}"

echo "==> Python syntax check"
"${PYTHON}" -m py_compile main.py faa_proxy.py platform_admin_identity.py platform_admin_auth.py

echo "==> Unit tests"
"${PYTHON}" -m unittest discover -s tests -p "test_*.py"

echo "==> Rollback-compatible database migrations"
"${PYTHON}" scripts/check_migration_compatibility.py

echo "==> Starting local tracker on http://${HOST}:${PORT}"
"${PYTHON}" -m uvicorn main:app --host "${HOST}" --port "${PORT}" >"${SERVER_LOG}" 2>&1 &
SERVER_PID="$!"

ready=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    echo "Local tracker exited before becoming ready. Server log:" >&2
    cat "${SERVER_LOG}" >&2
    exit 1
  fi
  if curl -fsS "http://${HOST}:${PORT}/versions" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done

if [ "${ready}" -ne 1 ]; then
  echo "Timed out waiting for local tracker at http://${HOST}:${PORT}. Server log:" >&2
  cat "${SERVER_LOG}" >&2
  exit 1
fi

echo "==> HTTP smoke tests"
curl -fsS -o /dev/null "http://${HOST}:${PORT}/"
curl -fsS -o /dev/null "http://${HOST}:${PORT}/r2c"
curl -fsS -o /dev/null "http://${HOST}:${PORT}/versions"
curl -fsS -o /dev/null "http://${HOST}:${PORT}/livez"
curl -fsS -o /dev/null "http://${HOST}:${PORT}/readyz"
DEPLOYMENT_UNAUTH_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' \
  "http://${HOST}:${PORT}/deployment-readiness")"
if [ "${DEPLOYMENT_UNAUTH_STATUS}" != "403" ]; then
  echo "Expected unauthenticated deployment readiness to return 403; received ${DEPLOYMENT_UNAUTH_STATUS}." >&2
  exit 1
fi
curl -fsS \
  -H "Authorization: Bearer ${DEPLOYMENT_GATE_KEY}" \
  "http://${HOST}:${PORT}/deployment-readiness" \
  | "${PYTHON}" -c 'import json, sys; data=json.load(sys.stdin); assert data["safe_to_deploy"] is True, data'
FAA_UNAUTH_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' \
  "http://${HOST}:${PORT}/faa/notams?latitude=39.1&longitude=-121.1&radius=2")"
if [ "${FAA_UNAUTH_STATUS}" != "403" ]; then
  echo "Expected unauthenticated FAA proxy request to return 403; received ${FAA_UNAUTH_STATUS}." >&2
  exit 1
fi

echo "==> /ws/r2c protocol smoke test"
"${PYTHON}" - "${HOST}" "${PORT}" "${API_KEY}" <<'PY'
import asyncio
import json
import sys

import websockets

host, port, api_key = sys.argv[1:4]


async def main():
    async with websockets.connect(
        f"ws://{host}:{port}/ws/r2c",
        additional_headers={
            "X-SAR-Token": api_key,
            "User-Agent": "RID2Caltopo/release-check",
        },
    ) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "mapId": "RELEASECHECK",
            "zoneId": "release-check-zone",
            "guid": "release-check-zone",
            "name": "Release Check",
            "lat": 39.1,
            "lng": -121.1,
            "caltopoRttMs": 123,
        }))
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        payload = json.loads(raw)
        if payload.get("type") != "hello_ack":
            raise AssertionError(f"expected hello_ack, received {payload}")
        if payload.get("mapId") != "RELEASECHECK":
            raise AssertionError(f"unexpected mapId in hello_ack: {payload}")
        if payload.get("heartbeatSec", 0) <= 0 or payload.get("leaseSec", 0) <= 0:
            raise AssertionError(f"missing heartbeat/lease settings: {payload}")
        await ws.send(json.dumps({
            "type": "heartbeat",
            "seq": 1,
            "lat": 39.1,
            "lng": -121.1,
            "caltopoRttMs": 123,
        }))
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError("timed out waiting for heartbeat_ack")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            payload = json.loads(raw)
            if payload.get("type") == "heartbeat_ack":
                break
        if payload.get("clientSeq") != 1:
            raise AssertionError(f"heartbeat_ack did not echo client seq: {payload}")


asyncio.run(main())
PY

echo "Release check passed."
