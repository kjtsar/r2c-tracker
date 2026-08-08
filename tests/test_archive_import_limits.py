import asyncio
import io
import tarfile
import unittest
from unittest.mock import patch

from starlette.datastructures import UploadFile

import main


class ArchiveImportLimitTest(unittest.TestCase):
    def test_upload_limit_rejects_before_unbounded_read(self):
        upload = UploadFile(filename="too-large.tgz", file=io.BytesIO(b"12345"))

        with self.assertRaisesRegex(ValueError, "upload limit"):
            asyncio.run(main.read_upload_with_limit(upload, max_bytes=4))

    def test_archive_rejects_oversized_flight_log_member(self):
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            member = tarfile.TarInfo("2026/08/flightlog_1_test.json")
            member.size = 5
            archive.addfile(member, io.BytesIO(b"12345"))
        archive_bytes.seek(0)

        with patch.object(main, "MAX_FLIGHT_LOG_BYTES", 4):
            with tarfile.open(fileobj=archive_bytes, mode="r:gz") as archive:
                with self.assertRaisesRegex(ValueError, "per-flight-log"):
                    main.reviewed_flight_archive_members(archive)

    def test_archive_rejects_excessive_member_count(self):
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            for name in ("ignored-1.txt", "ignored-2.txt"):
                member = tarfile.TarInfo(name)
                member.size = 0
                archive.addfile(member, io.BytesIO())
        archive_bytes.seek(0)

        with patch.object(main, "MAX_ARCHIVE_MEMBERS", 1):
            with tarfile.open(fileobj=archive_bytes, mode="r:gz") as archive:
                with self.assertRaisesRegex(ValueError, "more than 1 entries"):
                    main.reviewed_flight_archive_members(archive)

    def test_archive_returns_only_sorted_flight_logs(self):
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            for name in (
                "2026/08/flightlog_2.json",
                "notes.txt",
                "2026/08/flightlog_1.json",
            ):
                payload = b"{}"
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        archive_bytes.seek(0)

        with tarfile.open(fileobj=archive_bytes, mode="r:gz") as archive:
            members = main.reviewed_flight_archive_members(archive)

        self.assertEqual(
            ["2026/08/flightlog_1.json", "2026/08/flightlog_2.json"],
            [member.name for member in members],
        )


if __name__ == "__main__":
    unittest.main()
