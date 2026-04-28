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

    log_messages = []

    def _record(level, message, *args):
        if args:
            message = message % args
        log_messages.append((level, message))

    logger = types.SimpleNamespace(
        info=lambda message, *args, **kwargs: _record("info", message, *args),
        warning=lambda message, *args, **kwargs: _record("warning", message, *args),
    )
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
        "R2C_DB_CLEANUP_SEC": 86400,
        "R2C_SWEEP_SEC": 15,
    }
    exec(snippet, namespace)
    namespace["__log_messages__"] = log_messages
    return namespace["R2CZoneConnection"], namespace["R2CCoordinationHub"], log_messages


_, BaseHub, LOG_MESSAGES = load_coordination_classes()


class FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent_texts = []

    async def accept(self):
        self.accepted = True

    async def send_text(self, text: str):
        self.sent_texts.append(text)


class TestHub(BaseHub):
    def __init__(self):
        super().__init__()
        self.owner_state_updates = []
        self.owner_state_deletes = []
        self.recorded_sightings = []
        self.cleanup_runs = 0

    async def _load_state(self):
        return

    async def _cleanup_persisted_state(self):
        self.cleanup_runs += 1
        return

    async def _upsert_zone_state(self, *args, **kwargs):
        return

    async def _delete_zone_state(self, *args, **kwargs):
        return

    async def _delete_stale_zones(self, *args, **kwargs):
        return

    async def _upsert_owner_state(self, *args, **kwargs):
        self.owner_state_updates.append((args, kwargs))
        return

    async def _delete_owner_state(self, *args, **kwargs):
        self.owner_state_deletes.append((args, kwargs))
        return

    async def _record_sighting(self, *args, **kwargs):
        self.recorded_sightings.append((args, kwargs))
        return


class R2CProtocolEdgeCaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        LOG_MESSAGES.clear()
        self.hub = TestHub()
        self.ws_alpha = FakeWebSocket()
        self.ws_bravo = FakeWebSocket()
        self.ws_charlie = FakeWebSocket()
        self.ws_delta = FakeWebSocket()
        for ws in (self.ws_alpha, self.ws_bravo, self.ws_charlie, self.ws_delta):
            await self.hub.connect(ws)

        await self.hub.handle_message(self.ws_alpha, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "name": "Alpha",
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "name": "Bravo",
        })
        await self.hub.handle_message(self.ws_charlie, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-charlie",
            "guid": "zone-charlie",
            "name": "Charlie",
        })
        await self.hub.handle_message(self.ws_delta, {
            "type": "hello",
            "mapId": "MAP2",
            "zoneId": "zone-delta",
            "guid": "zone-delta",
            "name": "Delta",
        })

    async def test_first_sighting_prefers_shorter_distance_when_timestamps_match(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-DIST",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 50.0,
            "mappedId": "",
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-DIST",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "droneTs": 1000,
            "distanceFromZoneM": 20.0,
            "mappedId": "",
        })

        owner = self.hub._owners[("MAP1", "RID-DIST")]
        self.assertEqual("zone-bravo", owner["owner_guid"])

    async def test_first_sighting_prefers_mapped_id_when_timestamp_and_distance_match(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-MAPPED",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 20.0,
            "mappedId": "",
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-MAPPED",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "droneTs": 1000,
            "distanceFromZoneM": 20.0,
            "mappedId": "1SAR7DJ",
        })

        owner = self.hub._owners[("MAP1", "RID-MAPPED")]
        self.assertEqual("zone-bravo", owner["owner_guid"])
        self.assertEqual(2, owner["lease_seq"])

    async def test_first_sighting_uses_guid_lexical_tie_breaker_last(self):
        await self.hub.handle_message(self.ws_charlie, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-GUID",
            "zoneId": "zone-charlie",
            "guid": "zone-charlie",
            "droneTs": 1000,
            "distanceFromZoneM": 20.0,
            "mappedId": "",
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-GUID",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "droneTs": 1000,
            "distanceFromZoneM": 20.0,
            "mappedId": "",
        })

        owner = self.hub._owners[("MAP1", "RID-GUID")]
        self.assertEqual("zone-bravo", owner["owner_guid"])

    async def test_ownership_is_isolated_per_map(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-SHARED",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "",
        })
        await self.hub.handle_message(self.ws_delta, {
            "type": "first_sighting",
            "mapId": "MAP2",
            "remoteId": "RID-SHARED",
            "zoneId": "zone-delta",
            "guid": "zone-delta",
            "droneTs": 2000,
            "distanceFromZoneM": 99.0,
            "mappedId": "",
        })

        self.assertEqual("zone-alpha", self.hub._owners[("MAP1", "RID-SHARED")]["owner_guid"])
        self.assertEqual("zone-delta", self.hub._owners[("MAP2", "RID-SHARED")]["owner_guid"])

    async def test_heartbeat_only_extends_leases_for_matching_owner_guid(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-A",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "",
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-B",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "",
        })

        self.hub._owners[("MAP1", "RID-A")]["lease_expire_ms"] = 1
        self.hub._owners[("MAP1", "RID-B")]["lease_expire_ms"] = 2

        await self.hub.handle_message(self.ws_alpha, {
            "type": "heartbeat",
            "seq": 9,
        })

        self.assertGreater(self.hub._owners[("MAP1", "RID-A")]["lease_expire_ms"], 1)
        self.assertEqual(2, self.hub._owners[("MAP1", "RID-B")]["lease_expire_ms"])

    async def test_drone_lost_from_non_owner_does_not_clear_owner(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-LOST",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "",
        })

        await self.hub.handle_message(self.ws_bravo, {
            "type": "drone_lost",
            "mapId": "MAP1",
            "remoteId": "RID-LOST",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
        })

        self.assertIn(("MAP1", "RID-LOST"), self.hub._owners)
        self.assertEqual([], self.hub.owner_state_deletes)

    async def test_drone_lost_from_owner_clears_owner(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-OWNER-LOST",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "",
        })

        await self.hub.handle_message(self.ws_alpha, {
            "type": "drone_lost",
            "mapId": "MAP1",
            "remoteId": "RID-OWNER-LOST",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
        })

        self.assertNotIn(("MAP1", "RID-OWNER-LOST"), self.hub._owners)
        self.assertEqual("MAP1", self.hub.owner_state_deletes[0][0][0])
        self.assertEqual("RID-OWNER-LOST", self.hub.owner_state_deletes[0][0][1])

    async def test_relay_records_sighting_for_non_owner_zone(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-RELAY",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1SAR7DJ",
        })

        await self.hub.handle_message(self.ws_bravo, {
            "type": "sighting",
            "mapId": "MAP1",
            "remoteId": "RID-RELAY",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "droneTs": 2000,
            "lat": 39.3,
            "lng": -121.3,
            "altM": 120.0,
        })

        self.assertEqual(1, len(self.hub.recorded_sightings))
        args = self.hub.recorded_sightings[0][0]
        self.assertEqual("MAP1", args[0])
        self.assertEqual("RID-RELAY", args[1])
        self.assertEqual("zone-bravo", args[2])

    async def test_owner_reassignment_is_logged_with_remote_id_and_reason(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-LOG",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 2000,
            "distanceFromZoneM": 50.0,
            "mappedId": "",
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-LOG",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "droneTs": 1000,
            "distanceFromZoneM": 20.0,
            "mappedId": "1SAR7DJ",
        })

        owner_logs = [message for level, message in LOG_MESSAGES if "r2c owner_decision:" in message]
        self.assertTrue(any("remote_id=RID-LOG" in message for message in owner_logs))
        self.assertTrue(any("reason=candidate_better" in message for message in owner_logs))
        self.assertTrue(any("prev_owner_guid=zone-alpha" in message for message in owner_logs))
        self.assertTrue(any("chosen_owner_guid=zone-bravo" in message for message in owner_logs))

    async def test_lease_timeout_expiry_is_logged_with_remote_id(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-TIMEOUT",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "",
        })

        self.hub._owners[("MAP1", "RID-TIMEOUT")]["lease_expire_ms"] = 1
        await self.hub.expire_stale_entries()

        expiry_logs = [message for level, message in LOG_MESSAGES if "r2c owner_expired:" in message]
        self.assertTrue(any("remote_id=RID-TIMEOUT" in message for message in expiry_logs))
        self.assertTrue(any("reason=lease_timeout" in message for message in expiry_logs))

    async def test_cleanup_loop_runs_on_activation(self):
        await self.hub._cleanup_persisted_state_safe()
        self.assertEqual(1, self.hub.cleanup_runs)


if __name__ == "__main__":
    unittest.main()
