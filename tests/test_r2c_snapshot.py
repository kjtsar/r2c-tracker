import pathlib
import types
import unittest


def load_snapshot_helpers():
    main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    source = main_path.read_text()
    start = source.index("def format_elapsed_ms(")
    end = source.index("\nFILTER_TIMEZONE =")
    snippet = source[start:end]
    namespace = {
        "Optional": __import__("typing").Optional,
        "R2C_HEARTBEAT_SEC": 15,
    }
    exec(snippet, namespace)
    return namespace["format_elapsed_ms"], namespace["build_r2c_snapshot"]


format_elapsed_ms, build_r2c_snapshot = load_snapshot_helpers()


class SnapshotHelpersTest(unittest.TestCase):
    def test_format_elapsed_ms_handles_seconds_minutes_and_hours(self):
        self.assertEqual("0s", format_elapsed_ms(0))
        self.assertEqual("59s", format_elapsed_ms(59_900))
        self.assertEqual("2m 05s", format_elapsed_ms(125_000))
        self.assertEqual("2h 03m", format_elapsed_ms(7_380_000))

    def test_build_r2c_snapshot_groups_drones_under_current_owner(self):
        now_ms = 1_000_000
        zones = [
            types.SimpleNamespace(
                map_id="MAP2",
                zone_id="zone-b",
                guid="guid-b",
                name="Bravo",
                lat=39.2,
                lng=-121.2,
                caltopo_rtt_ms=2100,
                online=True,
                last_seen_ms=999_980,
            ),
            types.SimpleNamespace(
                map_id="MAP1",
                zone_id="zone-a",
                guid="guid-a",
                name="Alpha",
                lat=39.1,
                lng=-121.1,
                caltopo_rtt_ms=1200,
                online=True,
                last_seen_ms=969_000,
            ),
        ]
        owners = [
            types.SimpleNamespace(
                map_id="MAP1",
                remote_id="RID-2",
                owner_zone_id="zone-a",
                mapped_id="1SAR7DJ",
                lease_seq=3,
                lease_expire_ms=1_020_000,
                first_drone_ts=900_000,
                first_distance_m=12.3,
            ),
            types.SimpleNamespace(
                map_id="MAP1",
                remote_id="RID-1",
                owner_zone_id="zone-a",
                mapped_id="",
                lease_seq=1,
                lease_expire_ms=1_015_000,
                first_drone_ts=901_000,
                first_distance_m=25.0,
            ),
        ]

        snapshot = build_r2c_snapshot(zones, owners, now_ms)

        self.assertEqual(2, snapshot["map_count"])
        self.assertEqual(2, snapshot["zone_count"])
        self.assertEqual(2, snapshot["owned_drone_count"])
        self.assertEqual("MAP1", snapshot["maps"][0]["map_id"])
        self.assertEqual("quiet", snapshot["maps"][0]["zones"][0]["status"])
        self.assertEqual("1SAR7DJ", snapshot["maps"][0]["zones"][0]["owned_drones"][0]["mapped_id"])
        self.assertEqual("RID-1", snapshot["maps"][0]["zones"][0]["owned_drones"][1]["remote_id"])
        self.assertEqual("online", snapshot["maps"][1]["zones"][0]["status"])

    def test_build_r2c_snapshot_marks_disconnected_zone_from_persisted_state(self):
        now_ms = 1_000_000
        zones = [
            types.SimpleNamespace(
                map_id="MAP1",
                zone_id="zone-a",
                guid="guid-a",
                name="Alpha",
                lat=39.1,
                lng=-121.1,
                caltopo_rtt_ms=1200,
                online=False,
                last_seen_ms=999_995,
            ),
        ]

        snapshot = build_r2c_snapshot(zones, [], now_ms)

        self.assertEqual("disconnected", snapshot["maps"][0]["zones"][0]["status"])


class SnapshotTemplateTest(unittest.TestCase):
    def test_snapshot_template_stops_auto_reload_after_idle_timeout(self):
        template_path = pathlib.Path(__file__).resolve().parents[1] / "templates" / "r2c_snapshot.html"
        template = template_path.read_text()

        self.assertIn('document.body.dataset.autoReloadMs = "30000";', template)
        self.assertIn('document.body.dataset.autoReloadIdleTimeoutMs = "300000";', template)
        self.assertIn("scheduleIdleAutoReloadStop", template)
        self.assertIn("stopAutoReload();", template)
        self.assertIn("document.visibilityState === 'hidden'", template)
        self.assertIn("document.addEventListener('visibilitychange', syncAutoReloadWithVisibility);", template)

    def test_r2c_route_disables_shared_live_refresh_websocket(self):
        main_path = pathlib.Path(__file__).resolve().parents[1] / "main.py"
        source = main_path.read_text()

        self.assertIn('"enable_live_refresh": False,', source)


if __name__ == "__main__":
    unittest.main()
