import asyncio
import os
import pathlib
import tarfile
import tempfile
import unittest
from datetime import datetime


class FakeApp:
    def get(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))

    def run(self):
        for func, args, kwargs in self.tasks:
            func(*args, **kwargs)


class FakeFileResponse:
    def __init__(self, path, media_type=None, filename=None):
        self.path = path
        self.media_type = media_type
        self.filename = filename


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def load_current_year_archive_endpoint():
    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()
    start = source.index('@app.get("/flightlogs/archive/current-year"')
    end = source.index("\n@app.", start + 1)
    snippet = source[start:end]
    namespace = {
        "app": FakeApp(),
        "os": os,
        "tarfile": tarfile,
        "datetime": datetime,
        "Response": object,
        "FileResponse": FakeFileResponse,
        "HTTPException": FakeHTTPException,
        "BackgroundTasks": FakeBackgroundTasks,
        "Depends": lambda dependency: dependency,
        "check_admin": lambda: True,
        "BASE_LOG_DIRECTORY": "",
    }
    exec(snippet, namespace)
    return namespace["download_current_year_flight_logs_archive"], namespace


class FlightlogArchiveDownloadTest(unittest.TestCase):
    def test_current_year_archive_file_exists_until_background_cleanup(self):
        endpoint, namespace = load_current_year_archive_endpoint()
        with tempfile.TemporaryDirectory() as tmp_dir:
            year = datetime.now().strftime("%Y")
            month_dir = os.path.join(tmp_dir, year, "05")
            os.makedirs(month_dir)
            flightlog_path = os.path.join(month_dir, "flightlog_1_test.json")
            with open(flightlog_path, "w") as fp:
                fp.write("{}")

            namespace["BASE_LOG_DIRECTORY"] = tmp_dir
            bg_tasks = FakeBackgroundTasks()

            response = asyncio.run(endpoint(bg_tasks, admin_user=True))

            self.assertTrue(os.path.exists(response.path))
            self.assertTrue(response.filename.startswith(f"r2c-tracker-flightlogs-{year}-archive_"))

            with tarfile.open(response.path, "r:gz") as archive:
                self.assertIn(f"{year}/05/flightlog_1_test.json", archive.getnames())

            bg_tasks.run()
            self.assertFalse(os.path.exists(response.path))


if __name__ == "__main__":
    unittest.main()
