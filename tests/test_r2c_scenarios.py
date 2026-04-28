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

    logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
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
        "R2C_SWEEP_SEC": 15,
    }
    exec(snippet, namespace)
    return namespace["R2CZoneConnection"], namespace["R2CCoordinationHub"]


_, BaseHub = load_coordination_classes()


class FakeWebSocket:
    def __init__(self, label: str):
        self.label = label
        self.accepted = False
        self.sent_texts = []

    async def accept(self):
        self.accepted = True

    async def send_text(self, text: str):
        self.sent_texts.append(text)


class ScenarioHub(BaseHub):
    def __init__(self):
        super().__init__()
        self.owner_upserts = []
        self.owner_deletes = []
        self.recorded_sightings = []

    async def _load_state(self):
        return

    async def _upsert_zone_state(self, *args, **kwargs):
        return

    async def _delete_zone_state(self, *args, **kwargs):
        return

    async def _delete_stale_zones(self, *args, **kwargs):
        return

    async def _upsert_owner_state(self, *args, **kwargs):
        self.owner_upserts.append((args, kwargs))
        return

    async def _delete_owner_state(self, *args, **kwargs):
        self.owner_deletes.append((args, kwargs))
        return

    async def _record_sighting(self, *args, **kwargs):
        self.recorded_sightings.append((args, kwargs))
        return


class ScenarioRunner:
    def __init__(self, hub: ScenarioHub):
        self.hub = hub
        self.websockets = {}

    async def register_zone(self, zone_id: str, map_id: str = "MAP1"):
        ws = FakeWebSocket(zone_id)
        self.websockets[zone_id] = ws
        await self.hub.connect(ws)
        await self.hub.handle_message(ws, {
            "type": "hello",
            "mapId": map_id,
            "zoneId": zone_id,
            "guid": zone_id,
            "name": zone_id.title(),
        })
        return ws

    async def send(self, zone_id: str, payload: dict):
        payload = dict(payload)
        payload.setdefault("guid", zone_id)
        payload.setdefault("zoneId", zone_id)
        await self.hub.handle_message(self.websockets[zone_id], payload)

    async def disconnect(self, zone_id: str):
        await self.hub.disconnect(self.websockets[zone_id])

    def owner_guid(self, map_id: str, remote_id: str) -> str:
        return self.hub._owners[(map_id, remote_id)]["owner_guid"]

    def owner_count_for_map(self, map_id: str) -> int:
        return sum(1 for key in self.hub._owners if key[0] == map_id)

    def owner_assignments(self):
        return {
            (map_id, remote_id): owner["owner_guid"]
            for (map_id, remote_id), owner in self.hub._owners.items()
        }

    def messages_for(self, zone_id: str, message_type: str):
        messages = []
        for text in self.websockets[zone_id].sent_texts:
            payload = json.loads(text)
            if payload.get("type") == message_type:
                messages.append(payload)
        return messages


class R2CScenarioSimulationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = ScenarioHub()
        self.runner = ScenarioRunner(self.hub)

    async def test_three_zone_claim_timeline_keeps_earliest_detection_as_owner(self):
        for zone_id in ("zone-alpha", "zone-bravo", "zone-charlie"):
            await self.runner.register_zone(zone_id)

        await self.runner.send("zone-bravo", {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-T1",
            "droneTs": 2000,
            "distanceFromZoneM": 15.0,
            "mappedId": "",
        })
        await self.runner.send("zone-charlie", {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-T1",
            "droneTs": 1500,
            "distanceFromZoneM": 40.0,
            "mappedId": "",
        })
        await self.runner.send("zone-alpha", {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-T1",
            "droneTs": 1000,
            "distanceFromZoneM": 120.0,
            "mappedId": "",
        })

        self.assertEqual("zone-alpha", self.runner.owner_guid("MAP1", "RID-T1"))
        owner_events = self.runner.messages_for("zone-alpha", "owner_assigned")
        self.assertEqual("zone-alpha", owner_events[-1]["ownerGuid"])
        self.assertEqual(3, owner_events[-1]["leaseSeq"])

    async def test_disconnect_then_expiry_allows_next_zone_to_take_over(self):
        for zone_id in ("zone-alpha", "zone-bravo"):
            await self.runner.register_zone(zone_id)

        await self.runner.send("zone-alpha", {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-HANDOFF",
            "droneTs": 1000,
            "distanceFromZoneM": 10.0,
            "mappedId": "1SAR7DJ",
        })
        await self.runner.disconnect("zone-alpha")

        self.assertEqual("zone-alpha", self.runner.owner_guid("MAP1", "RID-HANDOFF"))

        owner = self.hub._owners[("MAP1", "RID-HANDOFF")]
        owner["lease_expire_ms"] = 1
        zone = self.hub._zones_by_map["MAP1"]["zone-alpha"]
        zone.last_seen_ms = 1
        await self.hub.expire_stale_entries()

        self.assertNotIn(("MAP1", "RID-HANDOFF"), self.hub._owners)

        await self.runner.send("zone-bravo", {
            "type": "first_sighting",
            "mapId": "MAP1",
            "remoteId": "RID-HANDOFF",
            "droneTs": 2000,
            "distanceFromZoneM": 20.0,
            "mappedId": "",
        })

        self.assertEqual("zone-bravo", self.runner.owner_guid("MAP1", "RID-HANDOFF"))
        expired_events = self.runner.messages_for("zone-bravo", "owner_expired")
        self.assertEqual("zone-alpha", expired_events[-1]["prevOwnerGuid"])

    async def test_six_zone_multi_drone_scenario_never_creates_duplicate_active_owners(self):
        for zone_id in (
            "zone-alpha",
            "zone-bravo",
            "zone-charlie",
            "zone-delta",
            "zone-echo",
            "zone-foxtrot",
        ):
            await self.runner.register_zone(zone_id)

        # Establish initial ownership for ten drones.
        scripted_claims = [
            ("zone-alpha", "RID-01", 1000, 10.0, "1SAR7A"),
            ("zone-bravo", "RID-02", 1000, 10.0, "1SAR7B"),
            ("zone-charlie", "RID-03", 1000, 10.0, "1SAR7C"),
            ("zone-delta", "RID-04", 1000, 10.0, "1SAR7D"),
            ("zone-echo", "RID-05", 1000, 10.0, "1SAR7E"),
            ("zone-foxtrot", "RID-06", 1000, 10.0, "1SAR7F"),
            ("zone-alpha", "RID-07", 1000, 15.0, ""),
            ("zone-bravo", "RID-08", 1000, 15.0, ""),
            ("zone-charlie", "RID-09", 1000, 15.0, ""),
            ("zone-delta", "RID-10", 1000, 15.0, ""),
        ]
        for zone_id, remote_id, drone_ts, distance_m, mapped_id in scripted_claims:
            await self.runner.send(zone_id, {
                "type": "first_sighting",
                "mapId": "MAP1",
                "remoteId": remote_id,
                "droneTs": drone_ts,
                "distanceFromZoneM": distance_m,
                "mappedId": mapped_id,
            })

        # Add overlapping reports from non-owner zones to mimic field overlap.
        relay_events = [
            ("zone-bravo", "RID-01"),
            ("zone-charlie", "RID-01"),
            ("zone-alpha", "RID-02"),
            ("zone-delta", "RID-03"),
            ("zone-echo", "RID-04"),
            ("zone-foxtrot", "RID-05"),
            ("zone-alpha", "RID-06"),
            ("zone-echo", "RID-07"),
            ("zone-foxtrot", "RID-08"),
            ("zone-bravo", "RID-09"),
            ("zone-charlie", "RID-10"),
        ]
        for index, (zone_id, remote_id) in enumerate(relay_events, start=1):
            await self.runner.send(zone_id, {
                "type": "sighting",
                "mapId": "MAP1",
                "remoteId": remote_id,
                "droneTs": 2000 + index,
                "lat": 39.0 + (index / 100.0),
                "lng": -121.0 - (index / 100.0),
                "altM": 100.0 + index,
            })

        assignments = self.runner.owner_assignments()
        self.assertEqual(10, self.runner.owner_count_for_map("MAP1"))
        self.assertEqual(10, len({remote_id for (map_id, remote_id) in assignments if map_id == "MAP1"}))
        self.assertEqual(11, len(self.hub.recorded_sightings))
        self.assertEqual("zone-alpha", assignments[("MAP1", "RID-01")])
        self.assertEqual("zone-foxtrot", assignments[("MAP1", "RID-06")])

    async def test_identical_claims_remain_deterministic_across_repeat_runs(self):
        async def run_once():
            hub = ScenarioHub()
            runner = ScenarioRunner(hub)
            await runner.register_zone("zone-bravo")
            await runner.register_zone("zone-charlie")
            await runner.send("zone-charlie", {
                "type": "first_sighting",
                "mapId": "MAP1",
                "remoteId": "RID-TIE",
                "droneTs": 1000,
                "distanceFromZoneM": 20.0,
                "mappedId": "",
            })
            await runner.send("zone-bravo", {
                "type": "first_sighting",
                "mapId": "MAP1",
                "remoteId": "RID-TIE",
                "droneTs": 1000,
                "distanceFromZoneM": 20.0,
                "mappedId": "",
            })
            return runner.owner_guid("MAP1", "RID-TIE")

        first = await run_once()
        second = await run_once()

        self.assertEqual("zone-bravo", first)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
