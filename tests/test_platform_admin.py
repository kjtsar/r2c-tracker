import pathlib
import asyncio
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

import main
from control_plane import ControlPlaneStore
from platform_admin import (
    AggregateUsage,
    BigQueryBillingSnapshotProvider,
    CostBreakdown,
    allocate_platform_costs,
    build_illustrative_platform_snapshot,
    build_pending_platform_snapshot,
    public_snapshot_dict,
)
from platform_admin_identity import PlatformAdminIdentity
from platform_admin_auth import GoogleGmailAuthorization, GoogleIdentity


class StaticPlatformAdminIdentityProvider:
    def __init__(self, email="platform@example.test", generation="1"):
        self.identity = PlatformAdminIdentity(
            email=email,
            display_name="Platform Administrator",
            generation=generation,
        )

    async def get_current(self):
        return self.identity


class FakeGoogleOidcClient:
    is_configured = True

    def __init__(self, email="platform@example.test"):
        self.email = email

    def authorization_request(self, redirect_uri):
        return (
            "https://accounts.google.test/auth?state=test-state",
            {
                "state": "test-state",
                "nonce": "test-nonce",
                "verifier": "test-verifier",
            },
        )

    def exchange_code(self, **_kwargs):
        return GoogleIdentity(
            subject="google-subject-123",
            email=self.email,
            name="Platform Administrator",
        )

    def gmail_authorization_request(self, redirect_uri):
        return self.authorization_request(redirect_uri)

    def exchange_gmail_code(self, **_kwargs):
        return GoogleGmailAuthorization(
            identity=self.exchange_code(),
            refresh_token="gmail-refresh-token",
        )


class FakeEmailSender:
    is_configured = True

    def __init__(self):
        self.messages = []

    def send_password_setup(self, *, recipient, setup_url):
        self.messages.append((recipient, setup_url))


class FakeTable:
    def __init__(self, table_id):
        self.table_id = table_id


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return self.rows


class FakeBigQueryClient:
    def __init__(self, table_ids=(), rows=()):
        self.table_ids = table_ids
        self.rows = rows
        self.queries = []

    def list_tables(self, dataset_id):
        self.dataset_id = dataset_id
        return [FakeTable(table_id) for table_id in self.table_ids]

    def query(self, query):
        self.queries.append(query)
        return FakeQuery(self.rows)


class PlatformAdminSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)
        self.snapshot = build_illustrative_platform_snapshot(self.now)

    def test_costs_reconcile_to_actual_platform_cost(self):
        self.assertEqual(
            self.snapshot.actual_cost_mtd,
            self.snapshot.attributed_cost_mtd + self.snapshot.unallocated_cost_mtd,
        )
        organization_total = sum(
            (org.month_to_date_cost.total for org in self.snapshot.organizations),
            Decimal("0.00"),
        )
        self.assertEqual(self.snapshot.attributed_cost_mtd, organization_total)

    def test_public_payload_contains_no_tenant_content_fields(self):
        payload_text = repr(public_snapshot_dict(self.snapshot)).lower()
        forbidden_fields = (
            "flight_id",
            "incident",
            "remote_id",
            "map_id",
            "archive_relpath",
            "flight_log",
        )
        for forbidden_field in forbidden_fields:
            self.assertNotIn(forbidden_field, payload_text)

    def test_template_marks_all_values_as_illustrative(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        environment = Environment(
            loader=FileSystemLoader(repo / "templates"),
            autoescape=select_autoescape(),
        )
        environment.globals["tracker_version"] = "test"
        environment.globals["get_flashed_messages"] = lambda request: []
        environment.globals["template_navigation"] = main.template_navigation
        template = environment.get_template("platform_admin.html")

        rendered = template.render(
            request=object(),
            enable_live_refresh=False,
            platform_snapshot=public_snapshot_dict(self.snapshot),
            platform_admin=SimpleNamespace(email="admin@example.test"),
            account_csrf_token="test-token",
        )

        self.assertIn("Costs &amp; Organizations", rendered)
        self.assertIn("Design prototype:", rendered)
        self.assertIn("Organization and contact records are also illustrative.", rendered)
        self.assertIn("Tenant privacy boundary:", rendered)
        self.assertIn("not affiliated with or endorsed by CalTopo", rendered)
        self.assertIn("support of the Teams API", rendered)
        self.assertNotIn("NCSSAR", rendered)
        self.assertNotIn("Delete Entire Database", rendered)
        template_source = (repo / "templates" / "platform_admin.html").read_text()
        self.assertIn('placeholder="mySAR"', template_source)

    def test_template_distinguishes_pending_live_export_from_prototype(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        environment = Environment(
            loader=FileSystemLoader(repo / "templates"),
            autoescape=select_autoescape(),
        )
        environment.globals["tracker_version"] = "test"
        environment.globals["get_flashed_messages"] = lambda request: []
        environment.globals["template_navigation"] = main.template_navigation
        template = environment.get_template("platform_admin.html")
        snapshot = build_pending_platform_snapshot(
            "The first billing table has not arrived.",
            self.now,
        )

        rendered = template.render(
            request=object(),
            enable_live_refresh=False,
            platform_snapshot=public_snapshot_dict(snapshot),
            platform_admin=SimpleNamespace(email="admin@example.test"),
            account_csrf_token="test-token",
        )

        self.assertIn("Google Cloud Billing export", rendered)
        self.assertIn("The first billing table has not arrived.", rendered)
        self.assertNotIn("All organizations, contacts, costs", rendered)


class BigQueryBillingSnapshotProviderTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)

    def provider(self, client):
        return BigQueryBillingSnapshotProvider(
            client=client,
            export_project="r2c-tracker-platform",
            export_dataset="r2c_billing_export",
            included_project_ids=(
                "r2c-tracker-pilot",
                "r2c-tracker-platform",
            ),
        )

    def test_missing_export_table_is_pending_not_illustrative(self):
        snapshot = self.provider(FakeBigQueryClient()).load_snapshot(self.now)

        self.assertFalse(snapshot.is_illustrative)
        self.assertEqual("pending", snapshot.source_status)
        self.assertEqual(Decimal("0.00"), snapshot.actual_cost_mtd)
        self.assertEqual((), snapshot.organizations)

    def test_live_cost_is_unallocated_until_usage_meters_exist(self):
        through = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
        client = FakeBigQueryClient(
            table_ids=("gcp_billing_export_v1_013BAC_12404A_395D0E",),
            rows=(
                {
                    "billing_data_through": through,
                    "actual_cost_mtd": "12.50",
                    "compute_cost": "3.00",
                    "network_cost": "4.00",
                    "storage_cost": "1.00",
                    "database_cost": "2.00",
                },
            ),
        )

        snapshot = self.provider(client).load_snapshot(self.now)

        self.assertEqual("ready", snapshot.source_status)
        self.assertEqual(Decimal("12.500000"), snapshot.actual_cost_mtd)
        self.assertEqual(Decimal("0.00"), snapshot.attributed_cost_mtd)
        self.assertEqual(snapshot.actual_cost_mtd, snapshot.unallocated_cost_mtd)
        self.assertIn("project.id IN ('r2c-tracker-pilot', 'r2c-tracker-platform')", client.queries[0])
        self.assertIn("SELECT MAX(billing_period)", client.queries[0])
        self.assertNotIn("FORMAT_DATE('%Y%m', CURRENT_DATE('UTC'))", client.queries[0])
        self.assertNotIn("flight", client.queries[0].lower())

    def test_metered_costs_are_proportional_and_other_is_shared_equally(self):
        allocations, unallocated = allocate_platform_costs(
            CostBreakdown(
                compute=Decimal("9.00"),
                network=Decimal("6.00"),
                storage=Decimal("3.00"),
                database=Decimal("4.00"),
                other=Decimal("2.00"),
            ),
            {
                "one": AggregateUsage(
                    compute_units=Decimal("1"),
                    network_bytes=100,
                    storage_byte_days=30,
                    database_units=Decimal("1"),
                ),
                "two": AggregateUsage(
                    compute_units=Decimal("2"),
                    network_bytes=100,
                    turn_relay_bytes=200,
                    database_units=Decimal("3"),
                ),
            },
        )

        self.assertEqual(Decimal("3.000000"), allocations["one"].compute)
        self.assertEqual(Decimal("6.000000"), allocations["two"].compute)
        self.assertEqual(Decimal("1.500000"), allocations["one"].network)
        self.assertEqual(Decimal("4.500000"), allocations["two"].network)
        self.assertEqual(Decimal("3.000000"), allocations["one"].storage)
        self.assertEqual(Decimal("1.000000"), allocations["one"].database)
        self.assertEqual(Decimal("3.000000"), allocations["two"].database)
        self.assertEqual(Decimal("1.000000"), allocations["one"].other)
        self.assertEqual(Decimal("1.000000"), allocations["two"].other)
        self.assertEqual(Decimal("0.000000"), unallocated)

    def test_cost_category_without_usage_meter_is_shared_equally(self):
        allocations, unallocated = allocate_platform_costs(
            CostBreakdown(compute=Decimal("5.00"), network=Decimal("7.00")),
            {"one": AggregateUsage(compute_units=Decimal("1"))},
        )

        self.assertEqual(Decimal("5.000000"), allocations["one"].compute)
        self.assertEqual(Decimal("7.000000"), allocations["one"].network)
        self.assertEqual(Decimal("0.000000"), unallocated)

    def test_shared_cost_rounding_still_reconciles_to_the_google_bill(self):
        allocations, unallocated = allocate_platform_costs(
            CostBreakdown(other=Decimal("1.00")),
            {
                "one": AggregateUsage(),
                "two": AggregateUsage(),
                "three": AggregateUsage(),
            },
        )

        attributed = sum(
            (allocation.total for allocation in allocations.values()),
            Decimal("0"),
        )
        self.assertEqual(Decimal("1.00"), attributed)
        self.assertEqual(Decimal("0.000000"), unallocated)

    def test_latest_prior_period_remains_visible_and_is_marked_stale(self):
        through = datetime(2026, 6, 30, 23, 0, tzinfo=UTC)
        client = FakeBigQueryClient(
            table_ids=("gcp_billing_export_v1_013BAC_12404A_395D0E",),
            rows=(
                {
                    "billing_period": "202606",
                    "billing_data_through": through,
                    "actual_cost_mtd": "9.75",
                    "compute_cost": "3.00",
                    "network_cost": "2.00",
                    "storage_cost": "1.00",
                    "database_cost": "1.50",
                },
            ),
        )

        snapshot = self.provider(client).load_snapshot(self.now)

        self.assertEqual("stale", snapshot.source_status)
        self.assertEqual("2026-06", snapshot.billing_period)
        self.assertFalse(snapshot.billing_period_is_current)
        self.assertTrue(snapshot.billing_data_stale)
        self.assertEqual(Decimal("9.750000"), snapshot.actual_cost_mtd)
        self.assertEqual(Decimal("9.75"), snapshot.forecast_cost)
        self.assertIn("Latest available billing period", snapshot.source_message)

    def test_detailed_export_is_preferred_over_standard_export(self):
        client = FakeBigQueryClient(
            table_ids=(
                "gcp_billing_export_v1_013BAC_12404A_395D0E",
                "gcp_billing_export_resource_v1_013BAC_12404A_395D0E",
            ),
        )
        provider = self.provider(client)

        self.assertEqual(
            "gcp_billing_export_resource_v1_013BAC_12404A_395D0E",
            provider._billing_table_id(),
        )

    def test_invalid_billing_table_identifier_is_ignored(self):
        client = FakeBigQueryClient(
            table_ids=("gcp_billing_export_v1_valid`; SELECT 1; --",),
        )

        self.assertIsNone(self.provider(client)._billing_table_id())


class PlatformAdminAuthenticationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = pathlib.Path(self.temp_dir.name) / "control-plane.db"
        self.store = ControlPlaneStore(f"sqlite+aiosqlite:///{database_path}")
        asyncio.run(self.store.init())
        asyncio.run(
            self.store.ensure_platform_admin(
                email="platform@example.test",
                display_name="Platform Administrator",
                bootstrap_password="platform secret phrase",
            )
        )
        self.store_patch = patch.object(main, "control_plane_store", self.store)
        self.store_patch.start()
        self.identity_provider = StaticPlatformAdminIdentityProvider()
        self.identity_patch = patch.object(
            main,
            "platform_admin_identity_provider",
            self.identity_provider,
        )
        self.identity_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.identity_patch.stop()
        self.store_patch.stop()
        asyncio.run(self.store.dispose())
        self.temp_dir.cleanup()

    @staticmethod
    def form_token(response):
        import re
        match = re.search(r'name="form_token" value="([^"]+)"', response.text)
        if match is None:
            raise AssertionError("Form token not found")
        return match.group(1)

    def test_protected_route_redirects_to_email_login(self):
        response = self.client.get(
            "/platform-admin/organizations",
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertIn("/platform-admin/login", response.headers["location"])

    def test_tracker_admin_credentials_do_not_grant_platform_access(self):
        page = self.client.get("/platform-admin/login")
        self.assertNotIn("platform@example.test", page.text)
        self.assertNotIn("Administrator:", page.text)
        response = self.client.post(
            "/platform-admin/login",
            data={
                "form_token": self.form_token(page),
                "email": "tracker-admin@example.test",
                "password": "tracker-secret",
                "next": "/platform-admin/organizations",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/platform-admin/login", response.headers["location"])

    def test_platform_email_and_password_create_session(self):
        page = self.client.get("/platform-admin/login")
        response = self.client.post(
            "/platform-admin/login",
            data={
                "form_token": self.form_token(page),
                "email": "platform@example.test",
                "password": "platform secret phrase",
                "next": "/platform-admin/organizations",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual(
            "/platform-admin/organizations",
            response.headers["location"],
        )
        protected = self.client.get("/platform-admin/organizations")
        self.assertEqual(200, protected.status_code)

    def test_identity_rotation_invalidates_existing_session(self):
        page = self.client.get("/platform-admin/login")
        self.client.post(
            "/platform-admin/login",
            data={
                "form_token": self.form_token(page),
                "email": "platform@example.test",
                "password": "platform secret phrase",
                "next": "/platform-admin/organizations",
            },
            follow_redirects=False,
        )
        self.identity_provider.identity = PlatformAdminIdentity(
            email="replacement@example.test",
            display_name="Replacement Administrator",
            generation="2",
        )

        response = self.client.get(
            "/platform-admin/organizations",
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertIn("/platform-admin/login", response.headers["location"])
        login = self.client.get("/platform-admin/login")
        self.assertNotIn("replacement@example.test", login.text)

    def test_matching_verified_google_identity_creates_session(self):
        with patch.object(main, "google_oidc_client", FakeGoogleOidcClient()):
            start = self.client.get(
                "/platform-admin/google/start",
                follow_redirects=False,
            )
            self.assertEqual(303, start.status_code)
            callback = self.client.get(
                "/platform-admin/google/callback"
                "?code=test-code&state=test-state",
                follow_redirects=False,
            )

        self.assertEqual(303, callback.status_code)
        self.assertEqual(
            "/platform-admin/organizations",
            callback.headers["location"],
        )
        self.assertEqual(
            200,
            self.client.get("/platform-admin/organizations").status_code,
        )

    def test_non_authoritative_google_email_is_rejected(self):
        with patch.object(
            main,
            "google_oidc_client",
            FakeGoogleOidcClient("other@example.test"),
        ):
            self.client.get(
                "/platform-admin/google/start",
                follow_redirects=False,
            )
            callback = self.client.get(
                "/platform-admin/google/callback"
                "?code=test-code&state=test-state",
                follow_redirects=False,
            )

        self.assertEqual(303, callback.status_code)
        self.assertEqual("/platform-admin/login", callback.headers["location"])
        self.assertEqual(
            303,
            self.client.get(
                "/platform-admin/organizations",
                follow_redirects=False,
            ).status_code,
        )

    def test_authenticated_admin_can_authorize_send_only_gmail(self):
        page = self.client.get("/platform-admin/login")
        self.client.post(
            "/platform-admin/login",
            data={
                "form_token": self.form_token(page),
                "email": "platform@example.test",
                "password": "platform secret phrase",
                "next": "/platform-admin/account",
            },
            follow_redirects=False,
        )
        stored = []
        with (
            patch.object(main, "google_oidc_client", FakeGoogleOidcClient()),
            patch.object(
                main,
                "PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET",
                "projects/test/secrets/gmail-refresh",
            ),
            patch.object(
                main,
                "store_gmail_refresh_token",
                side_effect=stored.append,
            ),
        ):
            start = self.client.get(
                "/platform-admin/gmail/start",
                follow_redirects=False,
            )
            callback = self.client.get(
                "/platform-admin/google/callback?code=test-code&state=test-state",
                follow_redirects=False,
            )

        self.assertEqual(303, start.status_code)
        self.assertEqual("/platform-admin/account", callback.headers["location"])
        self.assertEqual(["gmail-refresh-token"], stored)

    def test_email_setup_link_sets_password_once(self):
        sender = FakeEmailSender()
        with patch.object(main, "platform_admin_email_sender", sender):
            login_page = self.client.get("/platform-admin/login")
            response = self.client.post(
                "/platform-admin/setup/request",
                data={
                    "form_token": self.form_token_for(
                        login_page,
                        "/platform-admin/setup/request",
                    ),
                    "email": "platform@example.test",
                },
                follow_redirects=False,
            )
            self.assertEqual(303, response.status_code)
            self.assertEqual(1, len(sender.messages))
            setup_url = sender.messages[0][1]
            self.assertEqual("", urlparse(setup_url).query)
            token = parse_qs(urlparse(setup_url).fragment)["token"][0]
            setup_page = self.client.get("/platform-admin/setup")
            configured = self.client.post(
                "/platform-admin/setup",
                data={
                    "form_token": self.form_token(setup_page),
                    "setup_token": token,
                    "new_password": "browser generated password",
                    "new_password_confirm": "browser generated password",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, configured.status_code)
        self.assertEqual(
            "/platform-admin/organizations",
            configured.headers["location"],
        )

        replay_page = self.client.get("/platform-admin/setup")
        replay = self.client.post(
            "/platform-admin/setup",
            data={
                "form_token": self.form_token(replay_page),
                "setup_token": token,
                "new_password": "replacement generated password",
                "new_password_confirm": "replacement generated password",
            },
            follow_redirects=False,
        )
        self.assertEqual("/platform-admin/login", replay.headers["location"])

    @staticmethod
    def form_token_for(response, action):
        import re
        pattern = (
            rf'<form class="auth-form" method="post" action="{re.escape(action)}">'
            r'.*?name="form_token" value="([^"]+)"'
        )
        match = re.search(pattern, response.text, re.DOTALL)
        if match is None:
            raise AssertionError(f"Form token for {action} not found")
        return match.group(1)


if __name__ == "__main__":
    unittest.main()
