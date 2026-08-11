import pathlib
import importlib.util
import unittest


class GuardedReleaseTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(__file__).resolve().parents[1]

    def test_app_exposes_health_and_protected_activity_gate(self):
        source = (self.root / "main.py").read_text()
        self.assertIn('@app.get("/livez")', source)
        self.assertIn('@app.get("/readyz")', source)
        self.assertIn('@app.get("/deployment-readiness")', source)
        self.assertIn("secrets.compare_digest(candidate, DEPLOYMENT_GATE_KEY)", source)
        self.assertIn("recent_coordination_zones", source)
        self.assertIn("active_video_streams", source)
        self.assertIn("active_video_requests", source)
        self.assertIn("probe_flightlog_storage", source)

    def test_cloud_deploy_wires_probe_and_dedicated_gate_secret(self):
        deploy = (self.root / "deploy.sh").read_text()
        pilot = (self.root / "deploy_pilot.sh").read_text()
        self.assertIn("DEPLOYMENT_GATE_KEY=${DEPLOYMENT_GATE_KEY_SECRET_NAME}:latest", deploy)
        self.assertIn('--startup-probe "httpGet.path=/livez', deploy)
        self.assertIn('--liveness-probe "httpGet.path=/livez', deploy)
        self.assertIn("r2c-deployment-gate-key", pilot)
        self.assertIn('"FLIGHTLOGS_STORAGE_REQUIRED"', deploy)

    def test_candidate_promotion_and_rollback_are_separate_commands(self):
        guard = (self.root / "scripts" / "release_guard.py").read_text()
        self.assertIn("refresh_staging_databases.sh", guard)
        self.assertIn("setup_pilot_staging.sh", guard)
        self.assertIn("deploy_staging.sh", guard)
        self.assertIn("staging_regression(staging_url", guard)
        self.assertIn('"CONTAINER_IMAGE": image_digest', guard)
        self.assertIn("candidate_digest != image_digest", guard)
        self.assertIn("websocket_smoke", guard)
        self.assertIn("Candidate preparation failed; removing ephemeral staging resources.", guard)
        self.assertIn("cleanup_pilot_staging.sh", guard)
        self.assertIn('"ACTIVATE_LATEST_REVISION": "0"', guard)
        self.assertIn('"REVISION_TAG": "candidate"', guard)
        self.assertIn("deployment_readiness(PUBLIC_URL", guard)
        self.assertIn("regression(candidate_url", guard)
        self.assertIn("--to-revisions=", guard)
        for name in (
            "deploy_candidate.sh",
            "test_candidate.sh",
            "promote_candidate.sh",
            "rollback_release.sh",
        ):
            self.assertTrue((self.root / name).exists())
            self.assertIn(".venv/bin/python", (self.root / name).read_text())

    def test_local_release_gate_checks_migration_rollback_compatibility(self):
        release_check = (self.root / "release_check.sh").read_text()
        self.assertIn("scripts/check_migration_compatibility.py", release_check)

    def test_staging_clone_lifecycle_is_explicitly_scoped(self):
        refresh = (self.root / "scripts" / "refresh_staging_databases.sh").read_text()
        cleanup = (self.root / "cleanup_pilot_staging.sh").read_text()
        setup = (self.root / "setup_pilot_staging.sh").read_text()

        self.assertIn("r2c_pilot_tracker", refresh)
        self.assertIn("r2c_stage_tracker", refresh)
        self.assertIn('"${PG_DUMP}" --format=custom', refresh)
        self.assertIn("pg_dump_major", refresh)
        self.assertIn("compute start-iap-tunnel", refresh)
        self.assertIn("cloud-sql-proxy", refresh)
        self.assertIn("allow-iap-postgres-r2c-staging", refresh)
        self.assertIn("r2c-release-staging", cleanup)
        self.assertNotIn("DROP DATABASE IF EXISTS r2c_pilot_tracker", cleanup)
        self.assertIn("r2c_stage_tracker_user", setup)
        self.assertIn("r2c_stage_control_user", setup)
        self.assertIn("POSTGRES_15", setup)
        self.assertIn("db-f1-micro", setup)

    def test_bootstrap_recognizes_old_revision_http_status_wording(self):
        spec = importlib.util.spec_from_file_location(
            "release_guard",
            self.root / "scripts" / "release_guard.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.is_bootstrap_gate_unavailable(
            RuntimeError("Expected HTTP 200 from URL; received 404: body")
        ))
        self.assertTrue(module.is_bootstrap_gate_unavailable(
            RuntimeError("Expected HTTP 200 from URL; received 503: body")
        ))
        self.assertFalse(module.is_bootstrap_gate_unavailable(
            RuntimeError("Deployment blocked by active use")
        ))


if __name__ == "__main__":
    unittest.main()
