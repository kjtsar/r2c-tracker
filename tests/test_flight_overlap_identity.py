import pathlib
import unittest
from typing import Optional


def load_overlap_identity_helpers():
    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()
    start = source.index("def normalize_csv_value(")
    end = source.index("\n\ndef parse_csv_float")
    snippet = source[start:end]
    namespace = {
        "Optional": Optional,
    }
    exec(snippet, namespace)
    return namespace["normalize_remote_id"], namespace["resolve_overlap_identity"]


normalize_remote_id, resolve_overlap_identity = load_overlap_identity_helpers()


class FlightOverlapIdentityHelperTest(unittest.TestCase):
    def test_remote_id_is_preferred_when_present(self):
        remote_id, sar_id, fallback_to_sar = resolve_overlap_identity(" rid-alpha ", "1sar7")

        self.assertEqual("RID-ALPHA", remote_id)
        self.assertEqual("1SAR7", sar_id)
        self.assertTrue(fallback_to_sar)

    def test_sar_id_is_used_when_remote_id_missing(self):
        remote_id, sar_id, fallback_to_sar = resolve_overlap_identity("", " 1sar7 ")

        self.assertEqual("", remote_id)
        self.assertEqual("1SAR7", sar_id)
        self.assertFalse(fallback_to_sar)

    def test_remote_id_normalization_is_uppercase_and_trimmed(self):
        self.assertEqual("1581F67QE239L00A00DE", normalize_remote_id(" 1581F67QE239L00A00DE \n"))
        self.assertEqual("", normalize_remote_id(None))


if __name__ == "__main__":
    unittest.main()
