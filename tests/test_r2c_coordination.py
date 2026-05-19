import asyncio
import json
import math
import pathlib
import re
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
    manager_broadcasts = []

    logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )

    async def _broadcast(*args, **kwargs):
        manager_broadcasts.append((args, kwargs))

    manager = types.SimpleNamespace(broadcast=_broadcast)
    namespace = {
        "asyncio": asyncio,
        "json": json,
        "math": math,
        "re": re,
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
    return namespace["R2CZoneConnection"], namespace["R2CCoordinationHub"], manager_broadcasts


_, BaseHub, MANAGER_BROADCASTS = load_coordination_classes()
R2C_LEASE_SEC = 45


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
    def __init__(self, confirmation_store=None, zone_store=None):
        super().__init__()
        self.zone_state_updates = []
        self.zone_state_deletes = []
        self.persisted_mapped_map_id = None
        self.confirmation_store = confirmation_store if confirmation_store is not None else {}
        self.zone_store = zone_store if zone_store is not None else {}

    async def _load_state(self):
        return

    async def _upsert_zone_state(self, *args, **kwargs):
        self.zone_state_updates.append((args, kwargs))
        if len(args) >= 11:
            map_id, zone_id, guid, name, lat, lng, caltopo_rtt_ms, online, last_seen_ms, reported_map_id, coordination_mode = args[:11]
            self.zone_store[(map_id, zone_id)] = {
                "mapId": map_id,
                "zoneId": zone_id,
                "guid": guid,
                "name": name,
                "lat": lat,
                "lng": lng,
                "caltopoRttMs": caltopo_rtt_ms,
                "online": online,
                "lastSeenMs": last_seen_ms,
                "reportedMapId": reported_map_id,
                "coordinationMode": coordination_mode,
            }
        return

    async def _delete_zone_state(self, *args, **kwargs):
        self.zone_state_deletes.append((args, kwargs))
        if len(args) >= 2:
            self.zone_store.pop((args[0], args[1]), None)
        return

    async def _delete_stale_zones(self, *args, **kwargs):
        return

    async def _upsert_owner_state(self, *args, **kwargs):
        return

    async def _delete_owner_state(self, *args, **kwargs):
        return

    async def _upsert_confirmation_state(self, map_id, event, confirmed_at_ms):
        stored = dict(event)
        stored["confirmedAtMs"] = confirmed_at_ms
        self.confirmation_store[(map_id, event["remoteId"])] = stored

    async def _delete_confirmation_state(self, map_id, remote_id):
        self.confirmation_store.pop((map_id, remote_id), None)

    async def _delete_confirmation_state_for_zone(self, map_id, guid, zone_id):
        for (stored_map_id, remote_id), event in list(self.confirmation_store.items()):
            confirmed_by_guid = event.get("confirmedByGuid") or event.get("guid") or ""
            event_zone_id = event.get("zoneId") or ""
            if stored_map_id == map_id and ((guid and confirmed_by_guid == guid) or (zone_id and event_zone_id == zone_id)):
                self.confirmation_store.pop((stored_map_id, remote_id), None)

    async def _load_recent_confirmation_events(self, map_id, now_ms):
        cutoff_ms = now_ms - self.CONFIRMATION_RETENTION_MS
        recent_zone_cutoff_ms = now_ms - (R2C_LEASE_SEC * 1000)
        active_zone_keys = set()
        for (stored_map_id, _), zone in self.zone_store.items():
            if stored_map_id != map_id or not zone.get("online"):
                continue
            if int(zone.get("lastSeenMs", 0) or 0) < recent_zone_cutoff_ms:
                continue
            active_zone_keys.add(zone.get("zoneId") or "")
            active_zone_keys.add(zone.get("guid") or "")
        return [
            dict(event)
            for (stored_map_id, _), event in self.confirmation_store.items()
            if stored_map_id == map_id and int(event.get("confirmedAtMs", 0) or 0) >= cutoff_ms
            and any(
                key and key in active_zone_keys
                for key in (
                    event.get("confirmedByGuid") or "",
                    event.get("guid") or "",
                    event.get("zoneId") or "",
                )
            )
        ]

    async def _record_sighting(self, *args, **kwargs):
        return

    async def _resolve_persisted_mapped_coordination_map_id(self, *args, **kwargs):
        return self.persisted_mapped_map_id


class R2CCoordinationHubTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        MANAGER_BROADCASTS.clear()
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

    async def test_nearby_standalone_instances_share_coordination_group(self):
        self.ws_alpha.sent_texts.clear()
        self.ws_bravo.sent_texts.clear()

        await self.hub.handle_message(self.ws_alpha, {
            "type": "hello",
            "mapId": "",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "name": "Alpha",
            "lat": 39.15306,
            "lng": -121.13296,
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "hello",
            "mapId": "profile:home-default:incident:Training:op:1",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "name": "Bravo",
            "lat": 39.15307,
            "lng": -121.13298,
        })

        alpha_map_id = self.hub._connections[self.ws_alpha].map_id
        bravo_map_id = self.hub._connections[self.ws_bravo].map_id
        self.assertEqual(alpha_map_id, bravo_map_id)
        self.assertTrue(alpha_map_id.startswith("Standalone_"))
        self.assertNotIn("Training", alpha_map_id)
        self.assertEqual("standalone", self.hub._connections[self.ws_alpha].coordination_mode)

        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "",
            "remoteId": "RID-STANDALONE",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 2000,
            "distanceFromZoneM": 25.0,
            "mappedId": "",
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "first_sighting",
            "mapId": "profile:home-default:incident:Other:op:9",
            "remoteId": "RID-STANDALONE",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "droneTs": 1000,
            "distanceFromZoneM": 40.0,
            "mappedId": "",
        })

        owner = self.hub._owners[(alpha_map_id, "RID-STANDALONE")]
        self.assertEqual("zone-bravo", owner["owner_guid"])

    async def test_nearby_standalone_instance_joins_real_map_group(self):
        ws_charlie = FakeWebSocket()
        await self.hub.connect(ws_charlie)

        await self.hub.handle_message(ws_charlie, {
            "type": "hello",
            "mapId": "",
            "zoneId": "zone-charlie",
            "guid": "zone-charlie",
            "name": "Charlie",
            "lat": 39.1001,
            "lng": -121.1001,
        })

        self.assertEqual("MAP1", self.hub._connections[ws_charlie].map_id)
        self.assertEqual("standalone", self.hub._connections[ws_charlie].coordination_mode)
        self.assertEqual("map", self.hub._connections[self.ws_alpha].coordination_mode)

        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-FORGOT-MAP",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 2000,
            "distanceFromZoneM": 50.0,
            "mappedId": "",
        })
        await self.hub.handle_message(ws_charlie, {
            "type": "first_sighting",
            "mapId": "",
            "remoteId": "RID-FORGOT-MAP",
            "zoneId": "zone-charlie",
            "guid": "zone-charlie",
            "droneTs": 1000,
            "distanceFromZoneM": 75.0,
            "mappedId": "",
        })

        self.assertEqual("zone-charlie", self.hub._owners[("MAP1", "RID-FORGOT-MAP")]["owner_guid"])

    async def test_standalone_rehomes_to_map_when_mapped_peer_appears_later(self):
        ws_charlie = FakeWebSocket()
        await self.hub.connect(ws_charlie)

        await self.hub.handle_message(ws_charlie, {
            "type": "hello",
            "mapId": "",
            "zoneId": "zone-charlie",
            "guid": "zone-charlie",
            "name": "Charlie",
            "lat": 40.0,
            "lng": -122.0,
        })
        standalone_map_id = self.hub._connections[ws_charlie].map_id
        self.assertTrue(standalone_map_id.startswith("Standalone_"))

        await self.hub.handle_message(self.ws_alpha, {
            "type": "hello",
            "mapId": "MAP-LATE",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "name": "Alpha",
            "lat": 40.0001,
            "lng": -122.0001,
        })
        await self.hub.handle_message(ws_charlie, {
            "type": "heartbeat",
            "seq": 2,
            "lat": 40.0002,
            "lng": -122.0002,
        })

        self.assertEqual("MAP-LATE", self.hub._connections[ws_charlie].map_id)
        self.assertNotIn(standalone_map_id, self.hub._zones_by_map)
        self.assertIn(((standalone_map_id, "zone-charlie"), {}), self.hub.zone_state_deletes)
        self.assertIn("zone-charlie", self.hub._zones_by_map["MAP-LATE"])

    async def test_standalone_rehomes_to_persisted_map_anchor(self):
        ws_charlie = FakeWebSocket()
        await self.hub.connect(ws_charlie)

        await self.hub.handle_message(ws_charlie, {
            "type": "hello",
            "mapId": "",
            "zoneId": "zone-charlie",
            "guid": "zone-charlie",
            "name": "Charlie",
            "lat": 39.15307,
            "lng": -121.13294,
        })
        standalone_map_id = self.hub._connections[ws_charlie].map_id
        self.hub.persisted_mapped_map_id = "4J0LF02"

        await self.hub.handle_message(ws_charlie, {
            "type": "heartbeat",
            "seq": 3,
            "lat": 39.15307,
            "lng": -121.13294,
        })

        self.assertEqual("4J0LF02", self.hub._connections[ws_charlie].map_id)
        self.assertIn(((standalone_map_id, "zone-charlie"), {}), self.hub.zone_state_deletes)

    async def test_drone_confirmed_broadcasts_to_all_zones_on_map(self):
        self.ws_alpha.sent_texts.clear()
        self.ws_bravo.sent_texts.clear()

        await self.hub.handle_message(self.ws_alpha, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-CONFIRM",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
            "org": "NCSSAR",
            "model": "Mavic 3",
            "ownerName": "Pilot"
        })

        for ws in (self.ws_alpha, self.ws_bravo):
            messages = [json.loads(text) for text in ws.sent_texts]
            confirmed = [msg for msg in messages if msg.get("type") == "drone_confirmed"]
            self.assertEqual(1, len(confirmed))
            self.assertEqual("RID-CONFIRM", confirmed[0]["remoteId"])
            self.assertEqual("1SAR7DJ", confirmed[0]["mappedId"])
            self.assertEqual("zone-alpha", confirmed[0]["confirmedByGuid"])

    async def test_drone_confirmed_broadcasts_repeated_remote_id_as_new_event(self):
        self.ws_alpha.sent_texts.clear()
        self.ws_bravo.sent_texts.clear()

        payload = {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-CONFIRM",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
            "org": "NCSSAR",
            "model": "Mavic 3",
            "ownerName": "Pilot"
        }

        await self.hub.handle_message(self.ws_alpha, payload)
        await self.hub.handle_message(self.ws_alpha, dict(payload, ownerName="Pilot 2"))

        for ws in (self.ws_alpha, self.ws_bravo):
            messages = [json.loads(text) for text in ws.sent_texts]
            confirmed = [msg for msg in messages if msg.get("type") == "drone_confirmed"]
            self.assertEqual(2, len(confirmed))
            self.assertEqual(["Pilot", "Pilot 2"], [msg["ownerName"] for msg in confirmed])

    async def test_drone_confirmed_replays_to_late_zone_on_same_map(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-LATE",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
            "org": "NCSSAR",
            "model": "Mavic 3",
            "ownerName": "Pilot"
        })

        ws_charlie = FakeWebSocket()
        await self.hub.connect(ws_charlie)
        await self.hub.handle_message(ws_charlie, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-charlie",
            "guid": "zone-charlie",
            "name": "Charlie",
            "lat": 39.3,
            "lng": -121.3,
        })

        messages = [json.loads(text) for text in ws_charlie.sent_texts]
        confirmed = [msg for msg in messages if msg.get("type") == "drone_confirmed"]
        self.assertEqual(1, len(confirmed))
        self.assertEqual("RID-LATE", confirmed[0]["remoteId"])
        self.assertEqual("1SAR7DJ", confirmed[0]["mappedId"])

    async def test_drone_confirmed_does_not_replay_after_owner_drone_lost(self):
        shared_confirmations = {}
        hub = TestHub(shared_confirmations, {})
        ws_alpha = FakeWebSocket()
        ws_bravo = FakeWebSocket()
        await hub.connect(ws_alpha)
        await hub.connect(ws_bravo)
        await hub.handle_message(ws_alpha, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "name": "Alpha",
            "lat": 39.1,
            "lng": -121.1,
        })
        await hub.handle_message(ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-ENDS",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1710000001000,
            "distanceFromZoneM": 10.0,
        })
        await hub.handle_message(ws_alpha, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-ENDS",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
        })
        await hub.handle_message(ws_alpha, {
            "type": "drone_lost",
            "mapId": "MAP1",
            "remoteId": "RID-ENDS",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
        })
        await hub.handle_message(ws_bravo, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "name": "Bravo",
            "lat": 39.2,
            "lng": -121.2,
        })

        messages = [json.loads(text) for text in ws_bravo.sent_texts]
        confirmed = [msg for msg in messages if msg.get("type") == "drone_confirmed"]
        self.assertEqual([], confirmed)
        self.assertNotIn(("MAP1", "RID-ENDS"), shared_confirmations)

    async def test_drone_confirmed_replays_across_hub_instances_on_hello(self):
        shared_confirmations = {}
        shared_zones = {}
        hub_a = TestHub(shared_confirmations, shared_zones)
        hub_b = TestHub(shared_confirmations, shared_zones)
        ws_alpha = FakeWebSocket()
        ws_bravo = FakeWebSocket()
        await hub_a.connect(ws_alpha)
        await hub_b.connect(ws_bravo)
        await hub_a.handle_message(ws_alpha, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "name": "Alpha",
            "lat": 39.1,
            "lng": -121.1,
        })
        await hub_b.handle_message(ws_bravo, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "name": "Bravo",
            "lat": 39.2,
            "lng": -121.2,
        })
        ws_bravo.sent_texts.clear()

        await hub_a.handle_message(ws_alpha, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-CROSS-INSTANCE",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
            "org": "NCSSAR",
            "model": "Mavic 3",
            "ownerName": "Pilot"
        })
        ws_bravo_reconnect = FakeWebSocket()
        await hub_b.connect(ws_bravo_reconnect)
        await hub_b.handle_message(ws_bravo_reconnect, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-bravo-reconnect",
            "guid": "zone-bravo-reconnect",
            "name": "Bravo",
            "lat": 39.2,
            "lng": -121.2,
        })

        messages = [json.loads(text) for text in ws_bravo_reconnect.sent_texts]
        confirmed = [msg for msg in messages if msg.get("type") == "drone_confirmed"]
        self.assertEqual(1, len(confirmed))
        self.assertEqual("RID-CROSS-INSTANCE", confirmed[0]["remoteId"])
        self.assertEqual("1SAR7DJ", confirmed[0]["mappedId"])

    async def test_drone_confirmed_does_not_replay_after_confirming_zone_disconnects(self):
        shared_confirmations = {}
        shared_zones = {}
        hub_a = TestHub(shared_confirmations, shared_zones)
        hub_b = TestHub(shared_confirmations, shared_zones)
        ws_alpha = FakeWebSocket()
        await hub_a.connect(ws_alpha)
        await hub_a.handle_message(ws_alpha, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "name": "Alpha",
            "lat": 39.1,
            "lng": -121.1,
        })
        await hub_a.handle_message(ws_alpha, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-STALE-CONFIRMATION",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
        })

        await hub_a.disconnect(ws_alpha)

        ws_bravo = FakeWebSocket()
        await hub_b.connect(ws_bravo)
        await hub_b.handle_message(ws_bravo, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "name": "Bravo",
            "lat": 39.2,
            "lng": -121.2,
        })

        messages = [json.loads(text) for text in ws_bravo.sent_texts]
        confirmed = [msg for msg in messages if msg.get("type") == "drone_confirmed"]
        self.assertEqual([], confirmed)
        self.assertNotIn(("MAP1", "RID-STALE-CONFIRMATION"), shared_confirmations)

    async def test_drone_confirmed_replays_when_standalone_zone_rehomes(self):
        ws_charlie = FakeWebSocket()
        await self.hub.connect(ws_charlie)

        await self.hub.handle_message(ws_charlie, {
            "type": "hello",
            "mapId": "",
            "zoneId": "zone-charlie",
            "guid": "zone-charlie",
            "name": "Charlie",
            "lat": 40.0,
            "lng": -122.0,
        })
        standalone_map_id = self.hub._connections[ws_charlie].map_id
        await self.hub.handle_message(ws_charlie, {
            "type": "drone_confirmed",
            "mapId": "",
            "remoteId": "RID-REHOME",
            "zoneId": "zone-charlie",
            "guid": "zone-charlie",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
            "org": "NCSSAR",
            "model": "Mavic 3",
            "ownerName": "Pilot"
        })
        self.ws_alpha.sent_texts.clear()
        ws_charlie.sent_texts.clear()

        await self.hub.handle_message(self.ws_alpha, {
            "type": "hello",
            "mapId": "MAP-LATE",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "name": "Alpha",
            "lat": 40.0001,
            "lng": -122.0001,
        })
        await self.hub.handle_message(ws_charlie, {
            "type": "heartbeat",
            "seq": 2,
            "lat": 40.0002,
            "lng": -122.0002,
        })

        self.assertEqual("MAP-LATE", self.hub._connections[ws_charlie].map_id)
        self.assertNotIn(standalone_map_id, self.hub._zones_by_map)
        alpha_messages = [json.loads(text) for text in self.ws_alpha.sent_texts]
        alpha_confirmed = [msg for msg in alpha_messages if msg.get("type") == "drone_confirmed"]
        self.assertEqual(1, len(alpha_confirmed))
        self.assertEqual("RID-REHOME", alpha_confirmed[0]["remoteId"])

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

        await self.hub.handle_message(self.ws_alpha, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "DRONE3",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
        })
        self.assertIn("DRONE3", self.hub._confirmed_drones_by_map.get("MAP1", {}))

        owner = self.hub._owners[("MAP1", "DRONE3")]
        owner["lease_expire_ms"] = 1
        alpha_conn = self.hub._zones_by_map["MAP1"]["zone-alpha"]
        alpha_conn.websocket = None
        alpha_conn.last_seen_ms = 1

        await self.hub.expire_stale_entries()

        self.assertNotIn(("MAP1", "DRONE3"), self.hub._owners)
        self.assertNotIn("DRONE3", self.hub._confirmed_drones_by_map.get("MAP1", {}))

    async def test_disconnect_marks_zone_offline_without_immediate_owner_expiry(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE4",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1sar7Dj"
        })

        await self.hub.disconnect(self.ws_alpha)

        self.assertIn("zone-alpha", self.hub._zones_by_map["MAP1"])
        self.assertIsNone(self.hub._zones_by_map["MAP1"]["zone-alpha"].websocket)
        self.assertIn(("MAP1", "DRONE4"), self.hub._owners)
        self.assertTrue(any(call[0][7] is False for call in self.hub.zone_state_updates if len(call[0]) >= 8))

    async def test_sighting_to_disconnected_owner_is_not_relayed(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE5",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1sar7Dj"
        })
        await self.hub.disconnect(self.ws_alpha)

        before = len(self.ws_alpha.sent_texts)
        await self.hub.handle_message(self.ws_bravo, {
            "type": "sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE5",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "droneTs": 1234,
            "lat": 39.3,
            "lng": -121.3,
            "altM": 120.0
        })

        self.assertEqual(before, len(self.ws_alpha.sent_texts))

    async def test_missing_caltopo_rtt_defaults_to_unknown_value(self):
        ws_charlie = FakeWebSocket()
        await self.hub.connect(ws_charlie)
        await self.hub.handle_message(ws_charlie, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-charlie",
            "guid": "zone-charlie",
            "name": "Charlie",
            "lat": 39.3,
            "lng": -121.3
        })

        conn = self.hub._zones_by_map["MAP1"]["zone-charlie"]
        self.assertEqual(0, conn.caltopo_rtt_ms)

    async def test_hello_sends_ack_with_timing_parameters(self):
        ack = json.loads(self.ws_alpha.sent_texts[0])

        self.assertEqual("hello_ack", ack["type"])
        self.assertEqual(15, ack["heartbeatSec"])
        self.assertEqual(45, ack["leaseSec"])

    async def test_heartbeat_sends_ack_and_echoes_client_seq(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE6",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1sar7Dj"
        })

        before = len(self.ws_alpha.sent_texts)
        await self.hub.handle_message(self.ws_alpha, {
            "type": "heartbeat",
            "seq": 7,
            "lat": 39.11,
            "lng": -121.11,
            "caltopoRttMs": 55,
        })

        ack = json.loads(self.ws_alpha.sent_texts[before])
        self.assertEqual("heartbeat_ack", ack["type"])
        self.assertEqual("MAP1", ack["mapId"])
        self.assertEqual("zone-alpha", ack["zoneId"])
        self.assertEqual("zone-alpha", ack["guid"])
        self.assertEqual(7, ack["clientSeq"])
        self.assertGreater(ack["ownerLeaseExpireTs"], ack["serverTime"])

    async def test_coordination_updates_do_not_trigger_generic_page_refresh(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE7",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1sar7Dj"
        })
        await self.hub.disconnect(self.ws_alpha)

        self.assertEqual([], MANAGER_BROADCASTS)


class TrackerTokenNormalizationTest(unittest.TestCase):
    def test_normalize_tracker_token_trims_whitespace(self):
        self.assertEqual("abc123", normalize_tracker_token("  abc123 \n"))
        self.assertEqual("", normalize_tracker_token(None))


if __name__ == "__main__":
    unittest.main()
