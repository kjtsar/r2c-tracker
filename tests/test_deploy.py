import json
import os
import pathlib
import re
import subprocess
import unittest


class DeployScriptTest(unittest.TestCase):
    def test_deploy_requires_app_version_code_argument(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["sh", "deploy.sh"],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Usage:", result.stderr)
        self.assertIn("R2C_RECOMMENDED_APP_VERSION_CODE", result.stderr)

    def test_deploy_rejects_non_numeric_app_version_code(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["sh", "deploy.sh", "1.5.5(77)"],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("positive integer", result.stderr)

    def test_deploy_rejects_mutable_container_image_reference(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["CONTAINER_IMAGE"] = "us-west1-docker.pkg.dev/project/repo/app:latest"
        result = subprocess.run(
            ["sh", "deploy.sh", "126"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("immutable Artifact Registry sha256 digest", result.stderr)

    def test_deploy_exports_app_version_policy_env_vars(self):
        script = (pathlib.Path(__file__).resolve().parents[1] / "deploy.sh").read_text()

        self.assertIn("R2C_RECOMMENDED_APP_VERSION_CODE=\"$1\"", script)
        self.assertIn("R2C_UPDATE_URL=\"${2:-}\"", script)
        self.assertIn("\"R2C_RECOMMENDED_APP_VERSION_CODE\": os.environ[\"R2C_RECOMMENDED_APP_VERSION_CODE\"]", script)
        self.assertIn("\"R2C_UPDATE_URL\": os.environ[\"R2C_UPDATE_URL\"]", script)
        self.assertIn("FAA_NOTAM_CLIENT_ID must be set", script)
        self.assertIn("FAA_NOTAM_CLIENT_SECRET must be set", script)
        self.assertIn("FAA_NOTAM_CLIENT_ID=${FAA_CLIENT_ID_SECRET_NAME}:latest", script)
        self.assertIn("FAA_NOTAM_CLIENT_SECRET=${FAA_CLIENT_SECRET_SECRET_NAME}:latest", script)
        self.assertIn("secret_has_enabled_version", script)
        self.assertIn("has no enabled version", script)
        self.assertIn("DATABASE_URL=${DATABASE_URL_SECRET_NAME}:latest", script)
        self.assertIn("TRACKER_ADMIN_PASS=${TRACKER_ADMIN_PASS_SECRET_NAME}:latest", script)
        self.assertNotIn("TRACKER_API_KEY", script)
        self.assertIn("DEPLOYMENT_GATE_KEY=${DEPLOYMENT_GATE_KEY_SECRET_NAME}:latest", script)
        self.assertIn(
            "CONTROL_PLANE_DATABASE_URL=${CONTROL_PLANE_DATABASE_URL_SECRET_NAME}:latest",
            script,
        )
        self.assertIn(
            "CONTROL_PLANE_SIGNING_KEY=${CONTROL_PLANE_SIGNING_KEY_SECRET_NAME}:latest",
            script,
        )
        self.assertIn("r2c-super-admin-identity", script)
        self.assertIn("dynamic platform administrator identity", script)
        self.assertNotIn("PLATFORM_ADMIN_EMAIL", script)
        self.assertNotIn("PLATFORM_ADMIN_PASS", script)
        self.assertIn(
            "GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID_SECRET_NAME}:latest",
            script,
        )
        self.assertIn(
            "GOOGLE_OAUTH_CLIENT_SECRET=${GOOGLE_OAUTH_CLIENT_SECRET_SECRET_NAME}:latest",
            script,
        )
        self.assertIn(
            "PLATFORM_EMAIL_SMTP_PASSWORD=${PLATFORM_EMAIL_SMTP_PASSWORD_SECRET_NAME}:latest",
            script,
        )
        self.assertIn(
            "PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN=${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME}:latest",
            script,
        )
        self.assertIn("roles/secretmanager.secretVersionAdder", script)
        self.assertIn('not os.environ.get("DATABASE_URL_SECRET_NAME")', script)
        self.assertIn('not os.environ.get("TRACKER_ADMIN_PASS_SECRET_NAME")', script)
        self.assertIn("--set-cloudsql-instances", script)
        self.assertIn("--clear-cloudsql-instances", script)
        self.assertIn('--network "${CLOUD_RUN_NETWORK}"', script)
        self.assertIn('--subnet "${CLOUD_RUN_SUBNET}"', script)
        self.assertIn('--vpc-egress "${CLOUD_RUN_VPC_EGRESS}"', script)
        self.assertIn("type=cloud-storage", script)
        self.assertIn("mount-path=/flightlogs-vol", script)
        self.assertIn('--no-traffic', script)
        self.assertIn('--tag "${REVISION_TAG}"', script)
        self.assertIn('--image "${CONTAINER_IMAGE}"', script)
        self.assertIn("immutable Artifact Registry sha256 digest", script)
        self.assertIn("RELEASE_STAGING_MODE", script)
        self.assertIn('clean_line.endswith(": pending review")', script)
        self.assertIn('clean_line.removesuffix(": pending review")', script)

    def test_container_includes_runtime_modules(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        dockerfile = (repo / "Dockerfile").read_text()
        for module in (
            "faa_proxy.py",
            "control_plane.py",
            "enrollment.py",
            "platform_admin.py",
            "platform_admin_identity.py",
            "platform_admin_auth.py",
            "stripe_checkout.py",
            "turn_credentials.py",
        ):
            with self.subTest(module=module):
                self.assertIn(f"COPY {module} .", dockerfile)

    def test_pilot_wrapper_refuses_other_projects_and_services(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        script = (repo / "deploy_pilot.sh").read_text()

        self.assertIn('GCLOUD_PROJECT}" != "r2c-tracker-pilot"', script)
        self.assertIn('SERVICE_NAME}" != "r2c-tracker-pilot"', script)
        self.assertIn('ALLOW_UNAUTHENTICATED}" != "1"', script)
        self.assertIn("CLOUDSDK_ACTIVE_CONFIG_NAME", script)
        self.assertIn("r2c-tracker-pilot-flightlogs", script)
        self.assertIn('CLOUD_RUN_NETWORK="${CLOUD_RUN_NETWORK:-r2c-pilot-vpc}"', script)
        self.assertIn(
            'CLOUD_RUN_SUBNET="${CLOUD_RUN_SUBNET:-r2c-pilot-us-west1}"',
            script,
        )
        self.assertIn('export CLOUD_SQL_INSTANCE="${CLOUD_SQL_INSTANCE:-}"', script)
        self.assertIn("https://api-staging.cgifederal-aim.com/nmsapi", script)
        self.assertIn("https://api-staging.cgifederal-aim.com/v1/auth/token", script)
        self.assertIn("r2c-google-oauth-client-id", script)
        self.assertIn("r2c-google-oauth-client-secret", script)
        self.assertIn("r2c-managed-request-ingest-key", script)
        self.assertIn('CONTROL_PLANE_MODE="${CONTROL_PLANE_MODE:-live}"', script)
        self.assertIn('RELEASE_STAGING_MODE="false"', script)
        self.assertIn('CONTROL_PLANE_MODE}" != "live"', script)
        self.assertIn("The production pilot must keep organization provisioning in live mode.", script)
        self.assertIn('PLATFORM_EMAIL_SMTP_HOST="${PLATFORM_EMAIL_SMTP_HOST:-}"', script)
        self.assertIn(
            'PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME="${PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME:-r2c-platform-email-gmail-refresh-token}"',
            script,
        )
        self.assertIn("stun:stun.cloudflare.com:3478", script)
        self.assertIn("r2c-cloudflare-turn-key-id", script)
        self.assertIn("r2c-cloudflare-turn-api-token", script)
        ice_default = re.search(
            r"export VIDEO_ICE_SERVERS_JSON='([^']+)'",
            script,
        )
        self.assertIsNotNone(ice_default)
        self.assertEqual(
            [{"urls": ["stun:stun.cloudflare.com:3478"]}],
            json.loads(ice_default.group(1)),
        )

    def test_staging_wrapper_is_private_and_uses_isolated_resources(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        script = (repo / "deploy_staging.sh").read_text()

        self.assertIn("r2c-tracker-staging", script)
        self.assertIn("r2c-staging-tracker-database-url", script)
        self.assertIn("r2c-staging-control-plane-database-url", script)
        self.assertIn("r2c-tracker-staging-flightlogs", script)
        self.assertIn("r2c-release-staging", script)
        self.assertIn('ALLOW_UNAUTHENTICATED="0"', script)
        self.assertIn('RELEASE_STAGING_MODE="true"', script)
        self.assertIn('CONTROL_PLANE_MODE="simulation"', script)
        self.assertIn('PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_SECRET_NAME=""', script)
        self.assertIn('STRIPE_SECRET_KEY_SECRET_NAME=""', script)

    def test_local_setup_uses_isolated_gcloud_configuration_and_private_env_file(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        script = (repo / "setup_pilot_local.sh").read_text()

        self.assertIn('CONFIG_NAME="${R2C_GCLOUD_CONFIG_NAME:-r2c-tracker-pilot}"', script)
        self.assertIn('gcloud --configuration="${CONFIG_NAME}"', script)
        self.assertIn(".env.pilot.local", script)
        self.assertIn('chmod 600 "${ENV_FILE}"', script)
        self.assertIn("auth print-access-token", script)
        self.assertIn("sqlite+aiosqlite:///./test.db", script)
        self.assertIn("sqlite+aiosqlite:///./control_plane.test.db", script)
        self.assertNotIn("cloud-sql-proxy", script)
        self.assertIn("FAA_NOTAM_CLIENT_ID", script)
        self.assertIn("FAA_NOTAM_CLIENT_SECRET", script)
        self.assertIn("CONTROL_PLANE_DATABASE_URL", script)
        self.assertIn("CONTROL_PLANE_SIGNING_KEY", script)
        self.assertNotIn("PLATFORM_ADMIN_EMAIL", script)
        self.assertNotIn("PLATFORM_ADMIN_PASS", script)

    def test_pilot_control_plane_setup_is_project_guarded_and_secret_backed(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        script = (repo / "setup_pilot_control_plane.sh").read_text()

        self.assertIn("Refusing to prepare the control plane", script)
        self.assertIn("r2c-control-plane-database-url", script)
        self.assertIn("r2c-control-plane-signing-key", script)
        self.assertIn("set_super_admin.sh", script)
        self.assertIn("roles/bigquery.jobUser", script)
        self.assertIn("roles/bigquery.dataViewer", script)

    def test_google_oauth_setup_validates_web_client_and_redirect_before_storage(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        script = (repo / "setup_google_oauth_secrets.sh").read_text()

        self.assertIn("r2c-google-oauth-client-id", script)
        self.assertIn("r2c-google-oauth-client-secret", script)
        self.assertIn(
            "https://r2c-tracker.com/platform-admin/google/callback",
            script,
        )
        self.assertIn(
            "https://r2c-tracker.com/google/callback",
            script,
        )
        self.assertIn('web.get("redirect_uris", [])', script)
        self.assertNotIn("echo ${oauth_client_secret}", script)


if __name__ == "__main__":
    unittest.main()
