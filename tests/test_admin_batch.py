import pathlib
import unittest


def load_admin_batch_parser():
    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()
    start = source.index("def parse_admin_batch_form(")
    end = source.index("\n\nTF = TimezoneFinder()")
    snippet = source[start:end]
    namespace = {}
    exec(snippet, namespace)
    return namespace["parse_admin_batch_form"]


parse_admin_batch_form = load_admin_batch_parser()


class FakeFormData:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        value = self.values.get(key, default)
        if isinstance(value, list):
            return value[0] if value else default
        return value

    def getlist(self, key):
        value = self.values.get(key, [])
        if isinstance(value, list):
            return value
        return [value]


class AdminBatchFormParsingTest(unittest.TestCase):
    def test_parse_batch_form_collects_updates_and_delete_ids(self):
        form = FakeFormData({
            "action": "delete_selected",
            "flight_ids": ["17", "18", "18", "bad"],
            "delete_ids": ["18", "99", "oops"],
            "sar_id_17": " 1sar10dj ",
            "uas_17": "  M3T ",
            "sar_id_18": " 1sar2ab ",
            "uas_18": " AIR3 ",
        })

        action, flight_ids, delete_ids, updates = parse_admin_batch_form(form)

        self.assertEqual("delete_selected", action)
        self.assertEqual([17, 18], flight_ids)
        self.assertEqual({18}, delete_ids)
        self.assertEqual({"sar_id": "1SAR10DJ", "uas": "m3t"}, updates[17])
        self.assertEqual({"sar_id": "1SAR2AB", "uas": "air3"}, updates[18])


if __name__ == "__main__":
    unittest.main()
