import pathlib
import unittest
from typing import Optional


def load_duration_formatter():
    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()
    start = source.index("def format_duration_hours(")
    end = source.index("\ndef datetime_from_format(")
    snippet = source[start:end]
    namespace = {
        "Optional": Optional,
    }
    exec(snippet, namespace)
    return namespace["format_duration_hours"]


format_duration_hours = load_duration_formatter()


class DurationFormatTest(unittest.TestCase):
    def test_formats_decimal_hours_as_hh_mm_ss(self):
        self.assertEqual("00:00:00", format_duration_hours(None))
        self.assertEqual("00:00:00", format_duration_hours(0))
        self.assertEqual("00:30:00", format_duration_hours(0.5))
        self.assertEqual("01:15:00", format_duration_hours(1.25))
        self.assertEqual("02:03:04", format_duration_hours(2 + 3 / 60 + 4 / 3600))

    def test_rounds_to_nearest_second_and_clamps_negative_values(self):
        self.assertEqual("00:00:04", format_duration_hours(0.001))
        self.assertEqual("00:00:00", format_duration_hours(-1))


if __name__ == "__main__":
    unittest.main()
