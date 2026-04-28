import asyncio
import pathlib
import unittest
from contextlib import asynccontextmanager
from typing import Optional


def load_submission_lock_helpers():
    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()
    start = source.index("_flight_submission_locks: dict[str, asyncio.Lock] = {}")
    end = source.index("\n\ndef load_recent_versions")
    snippet = source[start:end]
    namespace = {
        "asyncio": asyncio,
        "Optional": Optional,
        "asynccontextmanager": asynccontextmanager,
    }
    exec(snippet, namespace)
    return namespace["normalize_flight_submission_key"], namespace["serialized_flight_submission"]


normalize_flight_submission_key, serialized_flight_submission = load_submission_lock_helpers()


class FlightSubmissionSerializationTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_remote_id_is_serialized(self):
        events = []

        async def worker(name: str, delay_s: float):
            async with serialized_flight_submission(" 1581F67QE239L00A00DE ", "1sar7"):
                events.append(f"{name}-start")
                await asyncio.sleep(delay_s)
                events.append(f"{name}-end")

        await asyncio.gather(
            worker("alpha", 0.05),
            worker("bravo", 0.0),
        )

        self.assertEqual(
            ["alpha-start", "alpha-end", "bravo-start", "bravo-end"],
            events,
        )

    async def test_different_remote_ids_can_progress_independently(self):
        events = []

        async def worker(name: str, remote_id: str, sar_id: str, delay_s: float):
            async with serialized_flight_submission(remote_id, sar_id):
                events.append(f"{name}-start")
                await asyncio.sleep(delay_s)
                events.append(f"{name}-end")

        await asyncio.gather(
            worker("alpha", "RID-1", "1sar7", 0.05),
            worker("bravo", "RID-2", "1sar8", 0.0),
        )

        self.assertEqual(events[:2], ["alpha-start", "bravo-start"])

    def test_submission_key_is_normalized(self):
        self.assertEqual("RID:1581F67QE239L00A00DE", normalize_flight_submission_key(" 1581F67QE239L00A00DE ", "1sar7"))
        self.assertEqual("SAR:1SAR7", normalize_flight_submission_key("", " 1sar7 "))
        self.assertEqual("", normalize_flight_submission_key(None, None))


if __name__ == "__main__":
    unittest.main()
