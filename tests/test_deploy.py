import pathlib
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

    def test_deploy_exports_app_version_policy_env_vars(self):
        script = (pathlib.Path(__file__).resolve().parents[1] / "deploy.sh").read_text()

        self.assertIn("R2C_RECOMMENDED_APP_VERSION_CODE=\"$1\"", script)
        self.assertIn("R2C_UPDATE_URL=\"${2:-}\"", script)
        self.assertIn("\"R2C_RECOMMENDED_APP_VERSION_CODE\": os.environ[\"R2C_RECOMMENDED_APP_VERSION_CODE\"]", script)
        self.assertIn("\"R2C_UPDATE_URL\": os.environ[\"R2C_UPDATE_URL\"]", script)


if __name__ == "__main__":
    unittest.main()
