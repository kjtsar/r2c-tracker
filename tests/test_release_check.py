import pathlib
import unittest


class ReleaseCheckScriptTest(unittest.TestCase):
    def test_combined_qualification_runs_unit_suite_once_and_parallelizes_gates(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        combined = (root / "qualify_release.sh").read_text()
        release_check = (root / "release_check.sh").read_text()
        security_check = (root / "scripts" / "security_checks.sh").read_text()

        self.assertEqual(1, combined.count('unittest discover -s tests'))
        self.assertIn("./release_check.sh --skip-unit-tests", combined)
        self.assertIn("./scripts/security_checks.sh --skip-unit-tests", combined)
        self.assertIn('RELEASE_PID="$!"', combined)
        self.assertIn('SECURITY_PID="$!"', combined)
        self.assertIn('wait "${RELEASE_PID}"', combined)
        self.assertIn('wait "${SECURITY_PID}"', combined)
        self.assertIn('if [ "${release_status}" -ne 0 ]', combined)
        self.assertIn('if [ "${1:-}" = "--skip-unit-tests" ]', release_check)
        self.assertIn('if [ "${1:-}" = "--skip-unit-tests" ]', security_check)
        self.assertIn('AUDIT_PID="$!"', security_check)
        self.assertIn('BANDIT_PID="$!"', security_check)
        self.assertIn('SECRETS_PID="$!"', security_check)
        self.assertIn('SBOM_PID="$!"', security_check)
        self.assertIn('wait "${AUDIT_PID}"', security_check)
        self.assertIn('wait "${SBOM_PID}"', security_check)
        self.assertIn('if [ "${audit_status}" -ne 0 ]', security_check)

    def test_release_check_covers_local_server_and_protocol_smokes(self):
        script = (pathlib.Path(__file__).resolve().parents[1] / "release_check.sh").read_text()

        self.assertIn("unittest discover -s tests", script)
        self.assertIn("uvicorn main:app", script)
        self.assertIn("http://${HOST}:${PORT}/versions", script)
        self.assertIn("http://${HOST}:${PORT}/livez", script)
        self.assertIn("http://${HOST}:${PORT}/readyz", script)
        self.assertIn("/deployment-readiness", script)
        self.assertIn("/faa/notams?latitude=39.1&longitude=-121.1&radius=2", script)
        self.assertIn('FAA_UNAUTH_STATUS}" != "403"', script)
        self.assertIn("scripts/create_release_check_credential.py", script)
        self.assertIn("/{designator.lower()}/ws/r2c", script)
        self.assertIn("hello_ack", script)
        self.assertIn(
            "Run ./qualify_release.sh for complete pre-publication qualification.",
            script,
        )

        readme = (
            pathlib.Path(__file__).resolve().parents[1] / "README.md"
        ).read_text()
        self.assertIn(
            "The quickest complete local verification is:\n\n```bash\n"
            "./qualify_release.sh",
            readme,
        )
        self.assertIn("not complete pre-publication qualification", readme)

        main_source = (
            pathlib.Path(__file__).resolve().parents[1] / "main.py"
        ).read_text()
        self.assertIn('client_message == "unsubscribe"', main_source)

        guard = (
            pathlib.Path(__file__).resolve().parents[1]
            / "scripts"
            / "release_guard.py"
        ).read_text()
        self.assertNotIn("r2c-release-device-token", guard)
        self.assertIn("websocket_smoke", guard)
        self.assertIn("deployment-test-fixture", guard)
        self.assertNotIn("TRACKER_API_KEY", guard)

    def test_security_workflow_uses_current_node_runtime_actions(self):
        workflow = (
            pathlib.Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "security.yml"
        ).read_text()
        self.assertIn("actions/checkout@v7", workflow)
        self.assertIn("actions/setup-python@v7", workflow)
        self.assertIn("actions/upload-artifact@v7", workflow)
        self.assertNotIn("actions/checkout@v4", workflow)
        self.assertNotIn("actions/setup-python@v5", workflow)
        self.assertNotIn("actions/upload-artifact@v4", workflow)


if __name__ == "__main__":
    unittest.main()
