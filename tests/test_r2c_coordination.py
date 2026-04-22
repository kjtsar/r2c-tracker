import asyncio
import json
import pathlib
import types
import unittest
from datetime import UTC, datetime
from typing import Optional


def load_coordination_classes():
    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()
    start = source.index("class R2CZoneConnection:")
    end = source.index("\nr2c_hub = R2CCoordinationHub()")
    snippet = source[start:end]

    logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)
    manager = types.SimpleNamespace(broadcast=lambda *args, **kwargs: asyncio.sleep(0))
    namespace = {
        "asyncio": asyncio,
        "json": json,
        "Optional": Optional,
        "UTC": UTC,
        "datetime": datetime,
        "WebSocket": type("WebSocket", (), {}),
        "logger": logger,
        "manager": manager,
        "R2C_HEARTBEAT_SEC": 15,
        "R2C_LEASE_SEC": 45,
        "R2C_SWEEP_SEC": 15,
    }
    exec(snippet, namespace)
    return namespace["R2CZoneConnection"], namespace["R2CCoordinationHub"]


_, BaseHub = load_coordination_classes()


def load_token_helpers():
    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()
    start = source.index("def _mask_token(")
    end = source.index("\nR2C_SWEEP_SEC =")
    snippet = source[start:end]
    namespace = {
        "Optional": Optional,
    }
    exec(snippet, namespace)
    return namespace["_normalize_tracker_token"]


normalize_tracker_token = load_token_helpers()


class FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent_texts = []

    async def accept(self):
        self.accepted = True

    async def send_text(self, text: str):
        self.sent_texts.append(text)


class TestHub(BaseHub):
    async def _load_state(self):
        return

    async def _upsert_zone_state(self, *args, **kwargs):
        return

    async def _delete_zone_state(self, *args, **kwargs):
        return

    async def _delete_stale_zones(self, *args, **kwargs):
        return

    async def _upsert_owner_state(self, *args, **kwargs):
        return

    async def _delete_owner_state(self, *args, **kwargs):
        return

    async def _record_sighting(self, *args, **kwargs):
        return


class R2CCoordinationHubTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = TestHub()
        self.ws_alpha = FakeWebSocket()
        self.ws_bravo = FakeWebSocket()
        await self.hub.connect(self.ws_alpha)
        await self.hub.connect(self.ws_bravo)
        await self.hub.handle_message(self.ws_alpha, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "name": "Alpha",
            "lat": 39.1,
            "lng": -121.1
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "name": "Bravo",
            "lat": 39.2,
            "lng": -121.2
        })

    async def test_first_sighting_prefers_earlier_detection_then_distance(self):
        await self.hub.handle_message(self.ws_bravo, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE1",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "droneTs": 2000,
            "distanceFromZoneM": 25.0,
            "mappedId": ""
        })
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE1",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 100.0,
            "mappedId": ""
        })

        owner = self.hub._owners[("MAP1", "DRONE1")]
        self.assertEqual("zone-alpha", owner["owner_guid"])

    async def test_sighting_relay_goes_to_current_owner(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE2",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1sar7Dj"
        })

        before = len(self.ws_alpha.sent_texts)
        await self.hub.handle_message(self.ws_bravo, {
            "type": "sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE2",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "droneTs": 1234,
            "lat": 39.3,
            "lng": -121.3,
            "altM": 120.0
        })

        self.assertTrue(any("relay_sighting" in text for text in self.ws_alpha.sent_texts[before:]))

    async def test_sighting_from_owner_is_not_relayed_back_to_owner(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE2",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1sar7Dj"
        })

        before = len(self.ws_alpha.sent_texts)
        await self.hub.handle_message(self.ws_alpha, {
            "type": "sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE2",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1234,
            "lat": 39.3,
            "lng": -121.3,
            "altM": 120.0
        })

        self.assertFalse(any("relay_sighting" in text for text in self.ws_alpha.sent_texts[before:]))

    async def test_expire_stale_entries_expires_owner_without_heartbeat(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE3",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1sar7Dj"
        })

        owner = self.hub._owners[("MAP1", "DRONE3")]
        owner["lease_expire_ms"] = 1
        alpha_conn = self.hub._zones_by_map["MAP1"]["zone-alpha"]
        alpha_conn.websocket = None
        alpha_conn.last_seen_ms = 1

        await self.hub.expire_stale_entries()

        self.assertNotIn(("MAP1", "DRONE3"), self.hub._owners)


class TrackerTokenNormalizationTest(unittest.TestCase):
    def test_normalize_tracker_token_trims_whitespace(self):
        self.assertEqual("abc123", normalize_tracker_token("  abc123 \n"))
        self.assertEqual("", normalize_tracker_token(None))


if __name__ == "__main__":
    unittest.main()
