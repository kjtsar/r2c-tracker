#!/usr/bin/env python3
"""Guarded zero-traffic Cloud Run candidate, promotion, and rollback workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / ".release-state" / "pilot.json"
PROJECT = os.environ.get("GCLOUD_PROJECT", "r2c-tracker-pilot")
REGION = os.environ.get("REGION", "us-west1")
SERVICE = os.environ.get("SERVICE_NAME", "r2c-tracker-pilot")
PUBLIC_URL = os.environ.get("CONTROL_PLANE_PUBLIC_URL", "https://r2c-tracker.com").rstrip("/")
GATE_SECRET = os.environ.get("DEPLOYMENT_GATE_KEY_SECRET_NAME", "r2c-deployment-gate-key")
TRACKER_SECRET = os.environ.get("TRACKER_API_KEY_SECRET_NAME", "r2c-tracker-api-key")
CONFIG = os.environ.get("CLOUDSDK_ACTIVE_CONFIG_NAME", "r2c-tracker-pilot")


def run(*args: str, capture: bool = False, env: dict[str, str] | None = None) -> str:
    command = [str(arg) for arg in args]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=capture,
    )
    return completed.stdout.strip() if capture else ""


def gcloud(*args: str, capture: bool = False, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["gcloud", f"--configuration={CONFIG}", "--quiet", *args],
        cwd=ROOT,
        check=True,
        text=True,
        input=input_text,
        capture_output=capture or input_text is not None,
    )
    return completed.stdout.strip() if capture else ""


def service_description() -> dict:
    return json.loads(gcloud(
        "run", "services", "describe", SERVICE,
        "--project", PROJECT, "--region", REGION, "--format=json",
        capture=True,
    ))


def serving_revision(description: dict) -> str:
    traffic = description.get("status", {}).get("traffic", [])
    for item in traffic:
        if int(item.get("percent", 0) or 0) == 100 and item.get("revisionName"):
            return str(item["revisionName"])
    raise RuntimeError("Unable to identify the revision receiving 100% of traffic.")


def tagged_candidate(description: dict) -> tuple[str, str]:
    for item in description.get("status", {}).get("traffic", []):
        if item.get("tag") == "candidate" and item.get("revisionName") and item.get("url"):
            return str(item["revisionName"]), str(item["url"]).rstrip("/")
    raise RuntimeError("Cloud Run did not report the candidate tag and URL.")


def ensure_gate_secret() -> None:
    found = subprocess.run(
        ["gcloud", f"--configuration={CONFIG}", "secrets", "describe", GATE_SECRET,
         "--project", PROJECT],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not found:
        print(f"Creating dedicated deployment-gate secret {GATE_SECRET}...")
        gcloud("secrets", "create", GATE_SECRET, "--project", PROJECT,
               "--replication-policy=automatic")
        gcloud("secrets", "versions", "add", GATE_SECRET, "--project", PROJECT,
               "--data-file=-", input_text=secrets.token_urlsafe(48))


def secret_value(name: str) -> str:
    return gcloud(
        "secrets", "versions", "access", "latest", "--secret", name,
        "--project", PROJECT, capture=True,
    )


def request(url: str, *, bearer: str | None = None, expected: int = 200) -> tuple[int, bytes]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"Release checks require an HTTPS URL; received {url!r}.")
    headers = {"User-Agent": "r2c-release-guard/1"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        # The scheme and hostname are constrained above; urlopen is used only
        # for the explicitly resolved Cloud Run/custom-domain targets.
        with urlopen(Request(url, headers=headers), timeout=30) as response:  # nosec B310
            status_code, body = response.status, response.read()
    except HTTPError as exc:
        status_code, body = exc.code, exc.read()
    if status_code != expected:
        raise RuntimeError(f"Expected HTTP {expected} from {url}; received {status_code}: {body[:300]!r}")
    return status_code, body


def deployment_readiness(base_url: str, gate_key: str, *, storage_probe: bool = False) -> dict:
    suffix = "?storage_probe=true" if storage_probe else ""
    _, body = request(f"{base_url}/deployment-readiness{suffix}", bearer=gate_key)
    result = json.loads(body)
    if not result.get("safe_to_deploy"):
        raise RuntimeError(
            "Deployment blocked by active use: " + json.dumps(result.get("activity", {}), sort_keys=True)
        )
    print("Activity gate: idle (safe to continue).")
    return result


def is_bootstrap_gate_unavailable(error: RuntimeError) -> bool:
    message = str(error)
    return "received 404" in message or "received 503" in message


async def websocket_smoke(base_url: str, tracker_key: str) -> None:
    import websockets

    parsed = urlparse(base_url)
    websocket_url = f"wss://{parsed.netloc}/ws/r2c"
    async with websockets.connect(
        websocket_url,
        additional_headers={
            "X-SAR-Token": tracker_key,
            "User-Agent": "RID2Caltopo/cloud-release-check",
        },
        open_timeout=20,
    ) as websocket:
        await websocket.send(json.dumps({
            "type": "hello",
            "mapId": "RELEASECHECK",
            "zoneId": "cloud-release-check",
            "guid": "cloud-release-check",
            "name": "Cloud Release Check",
            "lat": 39.1,
            "lng": -121.1,
            "caltopoRttMs": 1,
        }))
        payload = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
        if payload.get("type") != "hello_ack":
            raise RuntimeError(f"Expected hello_ack; received {payload!r}")


def regression(base_url: str, expected_version: str = "") -> None:
    gate_key = secret_value(GATE_SECRET)
    tracker_key = secret_value(TRACKER_SECRET)
    request(f"{base_url}/deployment-readiness", expected=403)
    _, live_body = request(f"{base_url}/livez")
    _, ready_body = request(f"{base_url}/readyz")
    live = json.loads(live_body)
    ready = json.loads(ready_body)
    if live.get("status") != "ok" or ready.get("status") != "ready":
        raise RuntimeError(f"Candidate health checks failed: live={live!r} ready={ready!r}")
    if expected_version and live.get("version") != expected_version:
        raise RuntimeError(
            f"Candidate reports {live.get('version')!r}; expected {expected_version!r}."
        )
    deployment_readiness(base_url, gate_key, storage_probe=True)
    for path in ("/", "/r2c", "/versions"):
        request(f"{base_url}{path}")
    request(
        f"{base_url}/faa/notams?latitude=39.1&longitude=-121.1&radius=2",
        expected=403,
    )
    asyncio.run(websocket_smoke(base_url, tracker_key))
    print(f"Cloud regression passed against {base_url}.")


def load_state() -> dict:
    if not STATE_PATH.exists():
        raise RuntimeError(f"No candidate state found at {STATE_PATH}.")
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def deploy_candidate(app_version_code: int, bootstrap: bool) -> None:
    if run("git", "status", "--porcelain", capture=True):
        raise RuntimeError("Refusing to deploy an uncommitted or dirty worktree.")
    try:
        expected_version = run(
            "git", "describe", "--exact-match", "--tags", "HEAD", capture=True
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Refusing to deploy an untagged commit.") from exc
    ensure_gate_secret()
    description = service_description()
    previous_revision = serving_revision(description)
    gate_key = secret_value(GATE_SECRET)
    try:
        deployment_readiness(PUBLIC_URL, gate_key)
    except RuntimeError as exc:
        if not bootstrap:
            raise
        if not is_bootstrap_gate_unavailable(exc):
            raise
        print("Bootstrap acknowledged: the serving revision does not yet expose the deployment gate.")
    deploy_env = dict(os.environ)
    deploy_env.update({
        "ACTIVATE_LATEST_REVISION": "0",
        "REVISION_TAG": "candidate",
        "DEPLOYMENT_GATE_KEY_SECRET_NAME": GATE_SECRET,
    })
    run(str(ROOT / "deploy_pilot.sh"), str(app_version_code), env=deploy_env)
    candidate_revision, candidate_url = tagged_candidate(service_description())
    state = {
        "candidate_revision": candidate_revision,
        "candidate_url": candidate_url,
        "expected_version": expected_version,
        "previous_revision": previous_revision,
        "public_url": PUBLIC_URL,
        "bootstrap": bootstrap,
        "status": "candidate",
    }
    save_state(state)
    regression(candidate_url, expected_version)
    print(f"Candidate ready: {candidate_revision}")
    print("Production traffic is unchanged. Run ./promote_candidate.sh to go live.")


def test_candidate() -> None:
    state = load_state()
    regression(state["candidate_url"], state.get("expected_version", ""))


def promote_candidate(*, bootstrap: bool) -> None:
    state = load_state()
    if bootstrap and not state.get("bootstrap"):
        raise RuntimeError("Bootstrap promotion is allowed only for the recorded first gate release.")
    regression(state["candidate_url"], state.get("expected_version", ""))
    print("Waiting for the synthetic coordination heartbeat to age out...")
    time.sleep(2 * 15 + 2)
    try:
        deployment_readiness(state["public_url"], secret_value(GATE_SECRET))
    except RuntimeError as exc:
        if not bootstrap:
            raise
        if not is_bootstrap_gate_unavailable(exc):
            raise
        print("Bootstrap acknowledged: live activity was manually rechecked before promotion.")
    gcloud(
        "run", "services", "update-traffic", SERVICE,
        "--project", PROJECT, "--region", REGION,
        f"--to-revisions={state['candidate_revision']}=100",
    )
    try:
        _, body = request(f"{state['public_url']}/livez")
        request(f"{state['public_url']}/readyz")
        request(f"{state['public_url']}/versions")
        live = json.loads(body)
        if live.get("version") != state.get("expected_version"):
            raise RuntimeError(f"Live version verification failed: {live!r}")
    except Exception:
        print("Post-promotion verification failed; automatically restoring prior traffic.", file=sys.stderr)
        gcloud(
            "run", "services", "update-traffic", SERVICE,
            "--project", PROJECT, "--region", REGION,
            f"--to-revisions={state['previous_revision']}=100",
        )
        state["status"] = "automatic_rollback"
        save_state(state)
        raise
    state["status"] = "promoted"
    save_state(state)
    print(f"Promoted {state['candidate_revision']} to 100% production traffic.")


def rollback() -> None:
    state = load_state()
    target = state["previous_revision"]
    gcloud(
        "run", "services", "update-traffic", SERVICE,
        "--project", PROJECT, "--region", REGION,
        f"--to-revisions={target}=100",
    )
    request(f"{state['public_url']}/livez")
    state["status"] = "rolled_back"
    save_state(state)
    print(f"Rolled production traffic back to {target}.")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    deploy_parser = subparsers.add_parser("deploy-candidate")
    deploy_parser.add_argument("app_version_code", type=int)
    deploy_parser.add_argument("--bootstrap", action="store_true")
    subparsers.add_parser("test-candidate")
    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("--bootstrap", action="store_true")
    subparsers.add_parser("rollback")
    args = parser.parse_args()
    if args.command == "deploy-candidate":
        deploy_candidate(args.app_version_code, args.bootstrap)
    elif args.command == "test-candidate":
        test_candidate()
    elif args.command == "promote":
        promote_candidate(bootstrap=args.bootstrap)
    elif args.command == "rollback":
        rollback()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Release guard failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
