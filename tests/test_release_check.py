import pathlib
import unittest


class ReleaseCheckScriptTest(unittest.TestCase):
    def test_release_check_covers_local_server_and_protocol_smokes(self):
        script = (pathlib.Path(__file__).resolve().parents[1] / "release_check.sh").read_text()

        self.assertIn("unittest discover -s tests", script)
        self.assertIn("uvicorn main:app", script)
        self.assertIn("http://${HOST}:${PORT}/r2c", script)
        self.assertIn("http://${HOST}:${PORT}/versions", script)
        self.assertIn("ws://{host}:{port}/ws/r2c", script)
        self.assertIn("hello_ack", script)


if __name__ == "__main__":
    unittest.main()
