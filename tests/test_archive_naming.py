import pathlib
import unittest
from datetime import UTC, datetime


def load_archive_helpers():
    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()

    start = source.index("def archive_filename_for_flight(")
    end = source.index("\n\nTF = TimezoneFinder()")
    snippet = source[start:end]

    namespace = {
        "datetime": datetime,
        "Optional": __import__("typing").Optional,
        "os": __import__("os"),
        "re": __import__("re"),
    }
    exec(snippet, namespace)
    return (
        namespace["archive_filename_for_flight"],
        namespace["archive_relpath_for_flight"],
        namespace["parse_flight_id_from_archive_filename"],
    )


archive_filename_for_flight, archive_relpath_for_flight, parse_flight_id_from_archive_filename = load_archive_helpers()


class ArchiveNamingHelpersTest(unittest.TestCase):
    def test_archive_filename_includes_flight_id_prefix(self):
        ts = datetime(2026, 4, 20, 16, 45, 28, tzinfo=UTC)
        filename = archive_filename_for_flight(608, "1SAR118djav2_track", ts)
        self.assertTrue(filename.startswith("flightlog_608_20Apr2026_164528_UTC-"))

    def test_archive_relpath_uses_year_and_month(self):
        ts = datetime(2026, 4, 20, 16, 45, 28, tzinfo=UTC)
        relpath = archive_relpath_for_flight(608, "1SAR118djav2_track", ts)
        self.assertEqual("2026/04/flightlog_608_20Apr2026_164528_UTC-1SAR118djav2_track.json", relpath)

    def test_parse_flight_id_from_archive_filename(self):
        self.assertEqual(608, parse_flight_id_from_archive_filename("flightlog_608_20Apr2026_164528-foo.json"))
        self.assertIsNone(parse_flight_id_from_archive_filename("flightlog_20Apr2026_164528-foo.json"))


if __name__ == "__main__":
    unittest.main()
