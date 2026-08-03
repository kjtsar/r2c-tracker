import asyncio
import json
import math
import pathlib
import re
import types
import unittest
from datetime import datetime, timezone
from typing import Optional

UTC = timezone.utc


def load_coordination_classes():
    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()
    start = source.index("class R2CZoneConnection:")
    end = source.index("\nr2c_hub = R2CCoordinationHub()")
    snippet = source[start:end]
    manager_broadcasts = []

    logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )

    async def _broadcast(*args, **kwargs):
        manager_broadcasts.append((args, kwargs))

    manager = types.SimpleNamespace(broadcast=_broadcast)

    class FakeVideoIceServerProvider:
        async def get_ice_servers(self):
            return [{
                "urls": ["turns:turn.example.test:443?transport=tcp"],
                "username": "short-lived-user",
                "credential": "short-lived-password",
            }]

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
        "video_ice_server_provider": FakeVideoIceServerProvider(),
        "R2C_HEARTBEAT_SEC": 15,
        "R2C_LEASE_SEC": 45,
        "R2C_HEARTBEAT_ZONE_UPDATE_SEC": 60,
        "R2C_IDLE_PARK_SEC": 120,
        "R2C_RECOMMENDED_APP_VERSION_CODE": 77,
        "R2C_UPDATE_URL": "https://example.org/r2c",
        "R2C_RECOMMENDED_IOS_APP_BUILD_NUMBER": 12,
        "R2C_IOS_UPDATE_URL": "https://example.org/r2c-ios",
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
        self.owner_state_updates = []
        self.owner_state_deletes = []
        self.persisted_mapped_map_id = None
        self.confirmation_store = confirmation_store if confirmation_store is not None else {}
        self.zone_store = zone_store if zone_store is not None else {}

    async def _load_state(self):
        return

    async def _upsert_zone_state(self, *args, **kwargs):
        self.zone_state_updates.append((args, kwargs))
        if len(args) >= 11:
            map_id, zone_id, guid, name, lat, lng, caltopo_rtt_ms, online, last_seen_ms, reported_map_id, coordination_mode = args[:11]
            connection_state = args[11] if len(args) >= 12 else ("online" if online else "disconnected")
            app_version = args[12] if len(args) >= 13 else ""
            app_version_code = args[13] if len(args) >= 14 else 0
            self.zone_store[(map_id, zone_id)] = {
                "mapId": map_id,
                "zoneId": zone_id,
                "guid": guid,
                "name": name,
                "appVersion": app_version,
                "appVersionCode": app_version_code,
                "lat": lat,
                "lng": lng,
                "caltopoRttMs": caltopo_rtt_ms,
                "online": online,
                "lastSeenMs": last_seen_ms,
                "reportedMapId": reported_map_id,
                "coordinationMode": coordination_mode,
                "connectionState": connection_state,
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
        self.owner_state_updates.append((args, kwargs))
        return

    async def _delete_owner_state(self, *args, **kwargs):
        self.owner_state_deletes.append((args, kwargs))
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
            "appVersion": "1.5.5(77)",
            "appVersionCode": 77,
            "lat": 39.1,
            "lng": -121.1
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "name": "Bravo",
            "appVersion": "1.5.4(76)",
            "appVersionCode": 76,
            "lat": 39.2,
            "lng": -121.2
        })

    async def test_media_offer_includes_short_lived_ice_servers(self):
        self.hub._connections_by_device_credential_id["device-1"] = (
            types.SimpleNamespace(websocket=self.ws_alpha)
        )
        exchange = types.SimpleNamespace(
            request_id="request-1",
            device_credential_id="device-1",
            stream_session_id="stream-1",
            browser_offer_sdp="v=0\r\n",
            expires_at=datetime(2026, 7, 31, 22, 0, tzinfo=UTC),
        )

        delivered = await self.hub.send_video_media_offer(exchange)

        self.assertTrue(delivered)
        payload = json.loads(self.ws_alpha.sent_texts[-1])
        self.assertEqual("video_media_offer", payload["type"])
        self.assertEqual("request-1", payload["requestId"])
        self.assertEqual(
            "turns:turn.example.test:443?transport=tcp",
            payload["iceServers"][0]["urls"][0],
        )
        self.assertEqual(
            "short-lived-password",
            payload["iceServers"][0]["credential"],
        )

    async def test_device_reconnect_close_restores_still_live_connection(self):
        credential = types.SimpleNamespace(id="device-1")
        original = FakeWebSocket()
        replacement = FakeWebSocket()
        await self.hub.connect(original, credential)
        await self.hub.connect(replacement, credential)

        await self.hub.disconnect(replacement)

        self.assertIs(
            original,
            self.hub._connections_by_device_credential_id[
                "device-1"
            ].websocket,
        )

    async def test_video_delivery_finds_orphaned_live_device_connection(self):
        credential = types.SimpleNamespace(id="device-1")
        websocket = FakeWebSocket()
        await self.hub.connect(websocket, credential)
        self.hub._connections_by_device_credential_id.pop("device-1")
        exchange = types.SimpleNamespace(
            request_id="request-1",
            device_credential_id="device-1",
            browser_offer_sdp="v=0\r\n",
            expires_at=datetime(2026, 7, 31, 22, 0, tzinfo=UTC),
        )

        delivered = await self.hub.send_video_preflight_offer(exchange)

        self.assertTrue(delivered)
        self.assertEqual(
            "video_preflight_offer",
            json.loads(websocket.sent_texts[-1])["type"],
        )

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

    async def test_drone_confirmed_assigns_owner(self):
        self.ws_alpha.sent_texts.clear()
        self.ws_bravo.sent_texts.clear()

        await self.hub.handle_message(self.ws_bravo, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-SAVE-OWNER",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
        })

        owner = self.hub._owners[("MAP1", "RID-SAVE-OWNER")]
        self.assertEqual("zone-bravo", owner["owner_guid"])
        self.assertEqual("zone-bravo", owner["owner_zone_id"])
        self.assertEqual("1SAR7DJ", owner["mapped_id"])
        self.assertEqual(1, owner["lease_seq"])

        for ws in (self.ws_alpha, self.ws_bravo):
            messages = [json.loads(text) for text in ws.sent_texts]
            confirmed = [msg for msg in messages if msg.get("type") == "drone_confirmed"]
            assigned = [msg for msg in messages if msg.get("type") == "owner_assigned"]
            self.assertEqual(1, len(confirmed))
            self.assertEqual(1, len(assigned))
            self.assertEqual("RID-SAVE-OWNER", assigned[0]["remoteId"])
            self.assertEqual("zone-bravo", assigned[0]["ownerGuid"])
            self.assertEqual("zone-bravo", assigned[0]["ownerZoneId"])

    async def test_drone_confirmed_overrides_first_sighting_owner(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-SAVE-OVERRIDE",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "",
        })

        await self.hub.handle_message(self.ws_bravo, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-SAVE-OVERRIDE",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
        })

        owner = self.hub._owners[("MAP1", "RID-SAVE-OVERRIDE")]
        self.assertEqual("zone-bravo", owner["owner_guid"])
        self.assertEqual("zone-bravo", owner["owner_zone_id"])
        self.assertEqual(2, owner["lease_seq"])

    async def test_first_sighting_does_not_steal_confirmed_owner(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-SAVE-PROTECTED",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 2000,
            "distanceFromZoneM": 50.0,
            "mappedId": "",
        })
        await self.hub.handle_message(self.ws_bravo, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-SAVE-PROTECTED",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
        })
        self.ws_alpha.sent_texts.clear()
        self.ws_bravo.sent_texts.clear()

        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-SAVE-PROTECTED",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 1.0,
            "mappedId": "1SAR7DJ",
        })

        owner = self.hub._owners[("MAP1", "RID-SAVE-PROTECTED")]
        self.assertEqual("zone-bravo", owner["owner_guid"])
        self.assertEqual("zone-bravo", owner["owner_zone_id"])
        self.assertEqual(2, owner["lease_seq"])
        for ws in (self.ws_alpha, self.ws_bravo):
            messages = [json.loads(text) for text in ws.sent_texts]
            assigned = [msg for msg in messages if msg.get("type") == "owner_assigned"]
            self.assertEqual([], assigned)

    async def test_drone_lost_clears_confirmed_owner(self):
        await self.hub.handle_message(self.ws_bravo, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-SAVE-LOST",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
        })
        self.assertIn("RID-SAVE-LOST", self.hub._confirmed_drones_by_map.get("MAP1", {}))

        await self.hub.handle_message(self.ws_bravo, {
            "type": "drone_lost",
            "mapId": "MAP1",
            "remoteId": "RID-SAVE-LOST",
            "zoneId": "zone-bravo",
            "guid": "zone-bravo",
        })

        self.assertNotIn(("MAP1", "RID-SAVE-LOST"), self.hub._owners)
        self.assertNotIn("RID-SAVE-LOST", self.hub._confirmed_drones_by_map.get("MAP1", {}))
        self.assertNotIn(("MAP1", "RID-SAVE-LOST"), self.hub.confirmation_store)
        self.assertEqual("MAP1", self.hub.owner_state_deletes[-1][0][0])
        self.assertEqual("RID-SAVE-LOST", self.hub.owner_state_deletes[-1][0][1])

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

    async def test_sighting_from_owner_refreshes_owner_lease(self):
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
        owner = self.hub._owners[("MAP1", "DRONE2")]
        owner["lease_expire_ms"] = 1
        before_messages = len(self.ws_alpha.sent_texts)
        before_updates = len(self.hub.owner_state_updates)

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

        self.assertGreater(owner["lease_expire_ms"], 1)
        self.assertEqual(before_messages, len(self.ws_alpha.sent_texts))
        self.assertEqual(before_updates + 1, len(self.hub.owner_state_updates))

    async def test_sighting_from_non_owner_does_not_refresh_owner_lease(self):
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
        owner = self.hub._owners[("MAP1", "DRONE2")]
        owner["lease_expire_ms"] = 1

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

        self.assertEqual(1, owner["lease_expire_ms"])
        self.assertTrue(any("relay_sighting" in text for text in self.ws_alpha.sent_texts))

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

    async def test_disconnected_owner_lease_survives_until_expiry_sweep(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-DISCONNECT-LEASE",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1sar7Dj"
        })
        original_owner = dict(self.hub._owners[("MAP1", "RID-DISCONNECT-LEASE")])

        await self.hub.disconnect(self.ws_alpha)
        await self.hub.expire_stale_entries()

        self.assertEqual(original_owner, self.hub._owners[("MAP1", "RID-DISCONNECT-LEASE")])
        self.assertEqual([], [
            call for call in self.hub.owner_state_deletes
            if call[0][:2] == ("MAP1", "RID-DISCONNECT-LEASE")
        ])

        self.hub._owners[("MAP1", "RID-DISCONNECT-LEASE")]["lease_expire_ms"] = 1
        await self.hub.expire_stale_entries()

        self.assertNotIn(("MAP1", "RID-DISCONNECT-LEASE"), self.hub._owners)
        self.assertTrue(any(
            call[0][:2] == ("MAP1", "RID-DISCONNECT-LEASE")
            for call in self.hub.owner_state_deletes
        ))

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

    async def test_non_owner_heartbeat_does_not_extend_disconnected_owner_lease(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-NONOWNER-HEARTBEAT",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1sar7Dj"
        })
        owner = self.hub._owners[("MAP1", "RID-NONOWNER-HEARTBEAT")]
        original_expire_ms = owner["lease_expire_ms"]
        await self.hub.disconnect(self.ws_alpha)

        before = len(self.ws_bravo.sent_texts)
        await self.hub.handle_message(self.ws_bravo, {
            "type": "heartbeat",
            "seq": 77,
            "lat": 39.22,
            "lng": -121.22,
        })

        self.assertEqual(original_expire_ms, owner["lease_expire_ms"])
        ack = json.loads(self.ws_bravo.sent_texts[before])
        self.assertEqual("heartbeat_ack", ack["type"])
        self.assertEqual(0, ack["ownerLeaseExpireTs"])

    async def test_reconnect_replays_active_confirmation_but_not_expired_owner(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-VALID-REPLAY",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "mappedId": "1SAR7DJ",
            "trackLabel": "1SAR7DJ",
        })
        await self.hub.handle_message(self.ws_alpha, {
            "type": "drone_confirmed",
            "mapId": "MAP1",
            "remoteId": "RID-EXPIRED-REPLAY",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "mappedId": "1SAR8DJ",
            "trackLabel": "1SAR8DJ",
        })
        self.hub._owners[("MAP1", "RID-EXPIRED-REPLAY")]["lease_expire_ms"] = 1
        await self.hub.expire_stale_entries()

        ws_alpha_reconnect = FakeWebSocket()
        await self.hub.connect(ws_alpha_reconnect)
        await self.hub.handle_message(ws_alpha_reconnect, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-alpha-reconnect",
            "guid": "zone-alpha-reconnect",
            "name": "Alpha Reconnect",
            "lat": 39.1,
            "lng": -121.1,
        })

        messages = [json.loads(text) for text in ws_alpha_reconnect.sent_texts]
        confirmed_remote_ids = [
            message["remoteId"]
            for message in messages
            if message.get("type") == "drone_confirmed"
        ]
        self.assertEqual(["RID-VALID-REPLAY"], confirmed_remote_ids)

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
        self.assertTrue(ack["idleRecommended"])
        self.assertEqual(120, ack["idleParkSec"])
        self.assertEqual(77, ack["recommendedAppVersionCode"])
        self.assertEqual("https://example.org/r2c", ack["updateUrl"])

    async def test_ios_hello_uses_ios_specific_update_recommendation(self):
        ws_ios = FakeWebSocket()
        await self.hub.connect(ws_ios)
        await self.hub.handle_message(ws_ios, {
            "type": "hello",
            "mapId": "MAP1",
            "zoneId": "zone-ios",
            "guid": "zone-ios",
            "name": "iPad",
            "appPlatform": "ios",
            "appVersion": "1.2(11)",
            "appVersionCode": 11,
            "lat": 39.3,
            "lng": -121.3,
        })

        ack = json.loads(ws_ios.sent_texts[0])
        self.assertEqual(12, ack["recommendedAppVersionCode"])
        self.assertEqual("https://example.org/r2c-ios", ack["updateUrl"])

    async def test_hello_persists_and_broadcasts_app_version(self):
        state = self.hub.zone_store[("MAP1", "zone-alpha")]
        self.assertEqual("1.5.5(77)", state["appVersion"])
        self.assertEqual(77, state["appVersionCode"])

        zone_updates = [
            json.loads(message)
            for message in self.ws_bravo.sent_texts
            if json.loads(message).get("type") == "zone_update"
        ]
        alpha = next(
            zone
            for update in zone_updates
            for zone in update["zones"]
            if zone["zoneId"] == "zone-alpha"
        )
        self.assertEqual("1.5.5(77)", alpha["appVersion"])
        self.assertEqual(77, alpha["appVersionCode"])

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

    async def test_idle_heartbeats_throttle_zone_update_broadcasts(self):
        self.ws_alpha.sent_texts.clear()
        self.ws_bravo.sent_texts.clear()

        await self.hub.handle_message(self.ws_alpha, {
            "type": "heartbeat",
            "seq": 1,
            "lat": 39.11,
            "lng": -121.11,
            "caltopoRttMs": 55,
        })
        await self.hub.handle_message(self.ws_alpha, {
            "type": "heartbeat",
            "seq": 2,
            "lat": 39.12,
            "lng": -121.12,
            "caltopoRttMs": 56,
        })

        alpha_messages = [json.loads(text) for text in self.ws_alpha.sent_texts]
        bravo_messages = [json.loads(text) for text in self.ws_bravo.sent_texts]
        self.assertEqual([1, 2], [
            message["clientSeq"]
            for message in alpha_messages
            if message.get("type") == "heartbeat_ack"
        ])
        self.assertEqual(1, sum(1 for message in alpha_messages if message.get("type") == "zone_update"))
        self.assertEqual(1, sum(1 for message in bravo_messages if message.get("type") == "zone_update"))

    async def test_idle_message_marks_zone_idle_without_owner_activity(self):
        self.ws_alpha.sent_texts.clear()
        self.ws_bravo.sent_texts.clear()

        await self.hub.handle_message(self.ws_alpha, {
            "type": "idle",
            "reason": "no_active_drones",
        })

        state = self.hub.zone_store[("MAP1", "zone-alpha")]
        self.assertFalse(state["online"])
        self.assertEqual("idle", state["connectionState"])

        bravo_messages = [json.loads(text) for text in self.ws_bravo.sent_texts]
        zone_updates = [message for message in bravo_messages if message.get("type") == "zone_update"]
        self.assertEqual(1, len(zone_updates))
        alpha_zone = next(zone for zone in zone_updates[0]["zones"] if zone["zoneId"] == "zone-alpha")
        self.assertFalse(alpha_zone["online"])
        self.assertEqual("idle", alpha_zone["connectionState"])

    async def test_idle_message_is_ignored_while_zone_owns_active_drone(self):
        await self.hub.handle_message(self.ws_alpha, {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "DRONE-IDLE-GUARD",
            "zoneId": "zone-alpha",
            "guid": "zone-alpha",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1sar7Dj"
        })
        self.ws_alpha.sent_texts.clear()
        self.ws_bravo.sent_texts.clear()

        await self.hub.handle_message(self.ws_alpha, {
            "type": "idle",
            "reason": "no_active_drones",
        })

        state = self.hub.zone_store[("MAP1", "zone-alpha")]
        self.assertTrue(state["online"])
        self.assertEqual("online", state["connectionState"])
        bravo_messages = [json.loads(text) for text in self.ws_bravo.sent_texts]
        self.assertFalse(any(message.get("type") == "zone_update" for message in bravo_messages))

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
