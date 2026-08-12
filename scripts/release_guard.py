#!/usr/bin/env python3
"""Guarded zero-traffic Cloud Run candidate, promotion, and rollback workflow."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / ".release-state" / "pilot.json"
STAGING_STATE_PATH = ROOT / ".release-state" / "staging.json"
STAGING_REUSE_MAX_AGE_SECONDS = 24 * 60 * 60
PROJECT = os.environ.get("GCLOUD_PROJECT", "r2c-tracker-pilot")
REGION = os.environ.get("REGION", "us-west1")
SERVICE = os.environ.get("SERVICE_NAME", "r2c-tracker-pilot")
PUBLIC_URL = os.environ.get("CONTROL_PLANE_PUBLIC_URL", "https://r2c-tracker.com").rstrip("/")
GATE_SECRET = os.environ.get("DEPLOYMENT_GATE_KEY_SECRET_NAME", "r2c-deployment-gate-key")
CONFIG = os.environ.get("CLOUDSDK_ACTIVE_CONFIG_NAME", "r2c-tracker-pilot")
STAGING_SERVICE = "r2c-tracker-staging"
STAGING_GATE_SECRET = "r2c-staging-deployment-gate-key"  # pragma: allowlist secret


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


@contextmanager
def committed_source_snapshot():
    with tempfile.TemporaryDirectory(prefix="r2c-release-source-") as temporary_dir:
        temporary_path = Path(temporary_dir)
        archive_path = temporary_path / "source.tar"
        source_path = temporary_path / "source"
        source_path.mkdir()
        run("git", "archive", "--format=tar", f"--output={archive_path}", "HEAD")
        with tarfile.open(archive_path) as archive:
            archive.extractall(source_path, filter="data")
        yield source_path


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


def service_description(service: str = SERVICE) -> dict:
    return json.loads(gcloud(
        "run", "services", "describe", service,
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


def latest_revision(description: dict) -> tuple[str, str]:
    status = description.get("status", {})
    revision = str(status.get("latestReadyRevisionName", ""))
    url = str(status.get("url", "")).rstrip("/")
    if not revision or not url:
        raise RuntimeError("Cloud Run did not report a ready revision and URL.")
    return revision, url


def revision_image_digest(revision: str) -> str:
    description = json.loads(gcloud(
        "run", "revisions", "describe", revision,
        "--project", PROJECT, "--region", REGION, "--format=json",
        capture=True,
    ))
    digest = str(description.get("status", {}).get("imageDigest", ""))
    if "@sha256:" not in digest:
        raise RuntimeError(f"Revision {revision} did not report an immutable image digest.")
    return digest


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


def identity_token() -> str:
    return gcloud("auth", "print-identity-token", capture=True)


def request(
    url: str,
    *,
    bearer: str | None = None,
    serverless_bearer: str | None = None,
    expected: int = 200,
    method: str = "GET",
) -> tuple[int, bytes]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"Release checks require an HTTPS URL; received {url!r}.")
    headers = {"User-Agent": "r2c-release-guard/1"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if serverless_bearer:
        headers["X-Serverless-Authorization"] = f"Bearer {serverless_bearer}"
    try:
        # The scheme and hostname are constrained above; urlopen is used only
        # for the explicitly resolved Cloud Run/custom-domain targets.
        with urlopen(Request(url, headers=headers, method=method), timeout=30) as response:  # nosec B310
            status_code, body = response.status, response.read()
    except HTTPError as exc:
        status_code, body = exc.code, exc.read()
    if status_code != expected:
        raise RuntimeError(f"Expected HTTP {expected} from {url}; received {status_code}: {body[:300]!r}")
    return status_code, body


def deployment_readiness(
    base_url: str,
    gate_key: str,
    *,
    storage_probe: bool = False,
    serverless_bearer: str | None = None,
) -> dict:
    suffix = "?storage_probe=true" if storage_probe else ""
    _, body = request(
        f"{base_url}/deployment-readiness{suffix}",
        bearer=gate_key,
        serverless_bearer=serverless_bearer,
    )
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


async def websocket_smoke(
    base_url: str,
    designator: str,
    tracker_key: str,
    serverless_bearer: str,
) -> None:
    import websockets

    parsed = urlparse(base_url)
    websocket_url = f"wss://{parsed.netloc}/{designator}/ws/r2c"
    async with websockets.connect(
        websocket_url,
        additional_headers={
            "X-SAR-Token": tracker_key,
            "X-Serverless-Authorization": f"Bearer {serverless_bearer}",
            "User-Agent": "RID2Caltopo/staging-release-check",
        },
        open_timeout=20,
    ) as websocket:
        await websocket.send(json.dumps({
            "type": "hello",
            "mapId": "RELEASECHECK",
            "zoneId": "staging-release-check",
            "guid": "staging-release-check",
            "name": "Staging Release Check",
            "lat": 39.1,
            "lng": -121.1,
            "caltopoRttMs": 1,
        }))
        payload = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
        if payload.get("type") != "hello_ack":
            raise RuntimeError(f"Expected hello_ack; received {payload!r}")


def staging_regression(base_url: str, expected_version: str) -> None:
    gate_key = secret_value(STAGING_GATE_SECRET)
    serverless_bearer = identity_token()
    _, live_body = request(f"{base_url}/livez", serverless_bearer=serverless_bearer)
    _, ready_body = request(f"{base_url}/readyz", serverless_bearer=serverless_bearer)
    live = json.loads(live_body)
    ready = json.loads(ready_body)
    if live.get("status") != "ok" or ready.get("status") != "ready":
        raise RuntimeError(f"Staging health checks failed: live={live!r} ready={ready!r}")
    if live.get("version") != expected_version:
        raise RuntimeError(
            f"Staging reports {live.get('version')!r}; expected {expected_version!r}."
        )
    deployment_readiness(
        base_url,
        gate_key,
        storage_probe=True,
        serverless_bearer=serverless_bearer,
    )
    _, fixture_body = request(
        f"{base_url}/deployment-test-fixture",
        bearer=gate_key,
        serverless_bearer=serverless_bearer,
        method="POST",
    )
    fixture = json.loads(fixture_body)
    asyncio.run(websocket_smoke(
        base_url,
        str(fixture["designator"]),
        str(fixture["device_token"]),
        serverless_bearer,
    ))
    print(f"Authenticated staging regression passed against {base_url}.")


def regression(base_url: str, expected_version: str = "") -> None:
    gate_key = secret_value(GATE_SECRET)
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
    for path in ("/", "/versions"):
        request(f"{base_url}{path}")
    request(
        f"{base_url}/faa/notams?latitude=39.1&longitude=-121.1&radius=2",
        expected=403,
    )
    print(f"Cloud regression passed against {base_url}.")


def load_state() -> dict:
    if not STATE_PATH.exists():
        raise RuntimeError(f"No candidate state found at {STATE_PATH}.")
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def save_staging_refresh_state() -> None:
    STAGING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STAGING_STATE_PATH.write_text(json.dumps({
        "instance": "r2c-release-staging",
        "project": PROJECT,
        "refreshed_at_epoch": int(time.time()),
        "region": REGION,
    }, indent=2, sort_keys=True) + "\n")


def validate_staging_reuse_state() -> dict:
    if not STAGING_STATE_PATH.exists():
        raise RuntimeError(
            "Staging reuse requires a refresh receipt from this workstation; "
            "run the first candidate without --reuse-staging."
        )
    try:
        state = json.loads(STAGING_STATE_PATH.read_text())
        refreshed_at = int(state["refreshed_at_epoch"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("The staging refresh receipt is invalid; clean up and recreate staging.") from exc
    expected = {
        "instance": "r2c-release-staging",
        "project": PROJECT,
        "region": REGION,
    }
    for key, value in expected.items():
        if state.get(key) != value:
            raise RuntimeError(
                f"The staging refresh receipt has unexpected {key}={state.get(key)!r}; "
                "clean up and recreate staging."
            )
    age_seconds = int(time.time()) - refreshed_at
    if age_seconds < 0 or age_seconds > STAGING_REUSE_MAX_AGE_SECONDS:
        raise RuntimeError(
            "The staging database clone is outside its 24-hour reuse window; "
            "run ./cleanup_pilot_staging.sh and create fresh staging."
        )
    return state


def deploy_candidate(app_version_code: int, bootstrap: bool, reuse_staging: bool) -> None:
    if run("git", "status", "--porcelain", capture=True):
        raise RuntimeError("Refusing to deploy an uncommitted or dirty worktree.")
    try:
        expected_version = run(
            "git", "describe", "--exact-match", "--tags", "HEAD", capture=True
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Refusing to deploy an untagged commit.") from exc
    if reuse_staging:
        validate_staging_reuse_state()
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
    setup_command = [str(ROOT / "setup_pilot_staging.sh")]
    if reuse_staging:
        setup_command.append("--reuse-existing")
    run(*setup_command)
    try:
        run(str(ROOT / "scripts" / "refresh_staging_databases.sh"))
        save_staging_refresh_state()
        staging_env = dict(os.environ)
        staging_env.update({
            "ACTIVATE_LATEST_REVISION": "1",
            "REVISION_TAG": "staging",
        })
        with committed_source_snapshot() as source_path:
            staging_env["DEPLOY_SOURCE_DIR"] = str(source_path)
            run(str(ROOT / "deploy_staging.sh"), str(app_version_code), env=staging_env)
        active_accounts = gcloud(
            "auth", "list", "--filter=status:ACTIVE", "--format=value(account)",
            capture=True,
        ).splitlines()
        if not active_accounts:
            raise RuntimeError("No active gcloud account is available for staging invocation.")
        active_account = active_accounts[0]
        gcloud(
            "run", "services", "add-iam-policy-binding", STAGING_SERVICE,
            "--project", PROJECT, "--region", REGION,
            f"--member=user:{active_account}", "--role=roles/run.invoker",
        )
        staging_revision, staging_url = latest_revision(
            service_description(STAGING_SERVICE)
        )
        image_digest = revision_image_digest(staging_revision)
        staging_regression(staging_url, expected_version)
        deploy_env = dict(os.environ)
        deploy_env.update({
            "ACTIVATE_LATEST_REVISION": "0",
            "REVISION_TAG": "candidate",
            "DEPLOYMENT_GATE_KEY_SECRET_NAME": GATE_SECRET,
            "CONTAINER_IMAGE": image_digest,
        })
        run(str(ROOT / "deploy_pilot.sh"), str(app_version_code), env=deploy_env)
        candidate_revision, candidate_url = tagged_candidate(service_description())
        candidate_digest = revision_image_digest(candidate_revision)
        if candidate_digest != image_digest:
            raise RuntimeError(
                "Production candidate image digest differs from the tested staging image."
            )
        state = {
            "candidate_revision": candidate_revision,
            "candidate_url": candidate_url,
            "expected_version": expected_version,
            "image_digest": image_digest,
            "previous_revision": previous_revision,
            "public_url": PUBLIC_URL,
            "staging_revision": staging_revision,
            "staging_url": staging_url,
            "bootstrap": bootstrap,
            "reused_staging": reuse_staging,
            "status": "candidate",
        }
        save_state(state)
        regression(candidate_url, expected_version)
    except Exception:
        if reuse_staging:
            print(
                "Candidate preparation failed; preserving explicitly reused staging resources for diagnosis or retry.",
                file=sys.stderr,
            )
            print(
                "Run ./cleanup_pilot_staging.sh when finished and within 24 hours of the latest database refresh.",
                file=sys.stderr,
            )
        else:
            print(
                "Candidate preparation failed; removing ephemeral staging resources.",
                file=sys.stderr,
            )
            subprocess.run(
                [str(ROOT / "cleanup_pilot_staging.sh")],
                cwd=ROOT,
                check=False,
            )
        raise
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
    deploy_parser.add_argument(
        "--reuse-staging",
        action="store_true",
        help="reuse validated isolated staging resources and refresh their database clones",
    )
    subparsers.add_parser("test-candidate")
    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("--bootstrap", action="store_true")
    subparsers.add_parser("rollback")
    args = parser.parse_args()
    if args.command == "deploy-candidate":
        deploy_candidate(args.app_version_code, args.bootstrap, args.reuse_staging)
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
