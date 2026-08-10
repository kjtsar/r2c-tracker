import pathlib
import unittest


class ReleaseCheckScriptTest(unittest.TestCase):
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

        guard = (
            pathlib.Path(__file__).resolve().parents[1]
            / "scripts"
            / "release_guard.py"
        ).read_text()
        self.assertIn("r2c-release-device-token", guard)
        self.assertIn("/{designator}/ws/r2c", guard)
        self.assertNotIn("TRACKER_API_KEY", guard)


if __name__ == "__main__":
    unittest.main()
