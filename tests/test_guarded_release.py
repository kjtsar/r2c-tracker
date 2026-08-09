import pathlib
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

    def test_local_release_gate_checks_migration_rollback_compatibility(self):
        release_check = (self.root / "release_check.sh").read_text()
        self.assertIn("scripts/check_migration_compatibility.py", release_check)


if __name__ == "__main__":
    unittest.main()
