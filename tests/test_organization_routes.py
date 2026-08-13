import asyncio
import csv
import html
import io
import json
import logging
import re
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketDisconnect

import main
from control_plane import (
    ControlPlaneStore,
    DeviceCredentialRecord,
    MANAGED_ACCESS_TERMS_VERSION,
    OrganizationUser,
    recording_link_code,
    VideoPreflightExchange,
    stream_link_code,
    tablet_link_code,
)
from enrollment import ControlPlaneTokenService
from platform_admin_identity import PlatformAdminIdentity
from platform_admin_auth import GoogleIdentity


class StaticPlatformAdminIdentityProvider:
    async def get_current(self):
        return PlatformAdminIdentity(
            email="platform@example.test",
            display_name="Platform Administrator",
            generation="1",
        )


class FakeGoogleOidcClient:
    is_configured = True

    def __init__(self, email="admin@ncssar.example"):
        self.email = email

    def authorization_request(self, redirect_uri):
        return (
            "https://accounts.google.test/auth?state=organization-state",
            {
                "state": "organization-state",
                "nonce": "organization-nonce",
                "verifier": "organization-verifier",
            },
        )

    def exchange_code(self, **_kwargs):
        return GoogleIdentity(
            subject="organization-google-subject",
            email=self.email,
            name="Primary Administrator",
        )


class FakeOrganizationEmailSender:
    is_configured = True

    def __init__(self):
        self.password_resets = []
        self.access_messages = []
        self.activation_messages = []
        self.member_activation_messages = []
        self.administrator_change_messages = []

    def send_organization_password_reset(self, **message):
        self.password_resets.append(message)

    def send_organization_access(self, **message):
        self.access_messages.append(message)

    def send_organization_activation(self, **message):
        self.activation_messages.append(message)

    def send_organization_member_activation(self, **message):
        self.member_activation_messages.append(message)

    def send_organization_administrator_changed(self, **message):
        self.administrator_change_messages.append(message)

    def send_organization_funding_exhausted(self, **message):
        self.access_messages.append(message)


HIDDEN_TOKEN_RE = re.compile(r'name="form_token" value="([^"]+)"')
ACTIVATION_LINK_RE = re.compile(
    r'href="(https://r2c-tracker\.com/ncssar/activate\?token=[^"]+)"'
)
QR_SRC_RE = re.compile(r'src="([^"]+/qr\.svg)"')
IDEMPOTENCY_RE = re.compile(r'name="idempotency_key"\s+value="([^"]+)"')
PLATFORM_FORM_TOKEN_RE = re.compile(
    r'<form class="onboarding-form".*?name="form_token" value="([^"]+)"',
    re.DOTALL,
)
STREAM_REQUEST_TOKEN_RE = re.compile(
    r'action="/ncssar/streams/[^"]+/request".*?'
    r'name="form_token" value="([^"]+)"',
    re.DOTALL,
)
logging.getLogger("httpx").setLevel(logging.WARNING)


class OrganizationRouteFlowTest(unittest.TestCase):
    def test_deployment_fixture_is_staging_only_and_gate_protected(self):
        production = self.client.post("/deployment-test-fixture")
        self.assertEqual(404, production.status_code)

        with (
            patch.object(main, "RELEASE_STAGING_MODE", True),
            patch.object(main, "DEPLOYMENT_GATE_KEY", "staging-gate-key"),
        ):
            unauthorized = self.client.post("/deployment-test-fixture")
            self.assertEqual(403, unauthorized.status_code)
            created = self.client.post(
                "/deployment-test-fixture",
                headers={"Authorization": "Bearer staging-gate-key"},
            )
            duplicate = self.client.post(
                "/deployment-test-fixture",
                headers={"Authorization": "Bearer staging-gate-key"},
            )

        self.assertEqual(200, created.status_code)
        self.assertEqual("releasecheck", created.json()["designator"])
        self.assertTrue(created.json()["device_token"].startswith("r2c_dev_"))
        self.assertEqual("no-store", created.headers["cache-control"])
        self.assertEqual(409, duplicate.status_code)

    def test_scoped_upload_route_is_registered(self):
        paths = {route.path for route in main.app.routes}
        self.assertIn("/{designator}/upload", paths)
        self.assertIn("/{designator}/ws/r2c", paths)
        self.assertNotIn("/upload", paths)
        self.assertNotIn("/ws/r2c", paths)
        self.assertNotIn("/ws", paths)
        self.assertNotIn("/r2c", paths)
        self.assertNotIn("/docs", paths)
        self.assertNotIn("/redoc", paths)
        self.assertNotIn("/openapi.json", paths)

    def test_scoped_upload_rejects_cross_organization_credential(self):
        credential = DeviceCredentialRecord(
            id="credential-1",
            organization_id="organization-1",
            designator="OTHER",
            device_name="Tablet",
            platform="ios",
            expires_at=datetime.now(),
        )
        with self.assertRaises(main.HTTPException) as raised:
            main.require_scoped_upload_credential("ncssar", credential)
        self.assertEqual(403, raised.exception.status_code)

    def test_scoped_upload_accepts_matching_credential(self):
        credential = DeviceCredentialRecord(
            id="credential-1",
            organization_id="organization-1",
            designator="NCSSAR",
            device_name="Tablet",
            platform="ios",
            expires_at=datetime.now(),
        )
        self.assertIs(
            credential,
            main.require_scoped_upload_credential("ncssar", credential),
        )

    def test_scoped_upload_reaches_archive_handler(self):
        credential = DeviceCredentialRecord(
            id="credential-1",
            organization_id="organization-1",
            designator="NCSSAR",
            device_name="Tablet",
            platform="ios",
            expires_at=datetime.now(),
        )

        async def matching_credential():
            return credential

        main.app.dependency_overrides[main.get_api_key] = matching_credential
        try:
            # An empty payload is rejected by the archive handler with 400;
            # the former missing organization route returned 404.
            response = self.client.put("/ncssar/upload", json={})
        finally:
            main.app.dependency_overrides.pop(main.get_api_key, None)
        self.assertEqual(400, response.status_code)


    def test_stream_event_socket_yields_to_form_navigation(self):
        script = Path("static/organization_streams_live.js").read_text()
        submit_listener = (
            'document.addEventListener("submit", stopForNavigation, true)'
        )
        self.assertIn(submit_listener, script)
        self.assertIn("stopped = true", script)
        self.assertLess(script.index(submit_listener), script.index("connect();"))

    def test_stream_event_socket_stops_without_focus(self):
        script = Path("static/organization_streams_live.js").read_text()

        self.assertIn("document.hasFocus()", script)
        self.assertIn('window.addEventListener("blur", handleBlur)', script)
        self.assertIn('window.addEventListener("focus", handleFocus)', script)
        self.assertIn("windowFocused = true", script)
        self.assertIn("function suspend()", script)
        self.assertIn("if (socket !== connectedSocket) return", script)

    def test_stream_event_socket_listens_while_focused_without_active_stream(self):
        script = Path("static/organization_streams_live.js").read_text()

        self.assertNotIn('state.dataset.active !== "true"', script)
        self.assertIn('message.type === "ready"', script)
        self.assertIn("status.membershipRevision !== renderedMembershipRevision", script)
        self.assertEqual(1, script.count("window.location.reload()"))
        self.assertIn("new Image()", script)
        self.assertIn("image.dataset.thumbnailRevision", script)

    def test_preflight_owns_request_refresh_until_one_decision_navigation(self):
        live_script = Path("static/organization_streams_live.js").read_text()
        preflight_script = Path("static/video_preflight.js").read_text()

        self.assertIn("function reloadForMembershipChange()", live_script)
        self.assertNotIn("preflightIsBusy", live_script)
        self.assertEqual(1, live_script.count("window.location.reload()"))
        self.assertIn("await waitForPilotDecision()", preflight_script)
        self.assertIn("renderRemoteQualityChooser(current)", preflight_script)
        self.assertIn("remote-control/approve", preflight_script)
        self.assertIn('["approved", "streaming"]', preflight_script)
        self.assertEqual(2, preflight_script.count("window.location.reload()"))

    def test_video_start_marker_retries_until_the_server_acknowledges_it(self):
        script = Path("static/video_media.js").read_text()

        self.assertIn("if (!response.ok) throw new Error", script)
        self.assertIn("startedReported = true", script)
        self.assertIn(
            "!startedReported && (videoBytesReceived > 0 || decodedFrames > 0)",
            script,
        )
        self.assertNotIn(
            "await reportStarted().catch(function () {});\n"
            "    await reportMetrics(true, false)",
            script,
        )
        self.assertLess(
            script.index("if (!response.ok) throw new Error"),
            script.index("startedReported = true"),
        )

    def test_video_end_reports_browser_reason(self):
        script = Path("static/video_media.js").read_text()

        self.assertIn("reason: message", script)
        self.assertIn("No video packets arrived", script)

    def test_video_decoder_stall_keeps_live_packet_flow_connected(self):
        script = Path("static/video_media.js").read_text()

        self.assertIn("videoBytesReceived > lastVideoBytes", script)
        self.assertIn("waiting for decoder recovery", script)
        self.assertIn("now - lastPacketProgressAt >= 15000", script)
        self.assertNotIn("now - lastFrameProgressAt >= 6000", script)

    def test_media_offer_posts_on_first_relay_candidate(self):
        script = Path("static/video_media.js").read_text()

        self.assertIn("waitForRelayCandidate(4000)", script)
        self.assertIn("relay_candidate_ms: relayCandidateMs", script)
        self.assertNotIn("waitForIce(5000)", script)

    def test_passive_stream_refresh_requires_an_advertised_r2c_stream(self):
        lingering_request = SimpleNamespace(
            id="request-1",
            device_name="Tablet 1",
            state="pending",
            route_kind="unknown",
            estimated_uplink_bps=0,
            selected_width=None,
            selected_height=None,
            selected_fps=None,
            selected_bitrate_bps=None,
            expires_at=datetime.now(),
        )

        status = main.organization_stream_status([], [lingering_request])

        self.assertFalse(status["active"])
        self.assertFalse(status["awaiting_approval"])

    def test_stream_status_marks_approval_wait_for_fast_reconciliation(self):
        waiting_request = SimpleNamespace(
            id="request-1",
            device_name="Tablet 1",
            state="awaiting_approval",
            route_kind="routed",
            estimated_uplink_bps=2_000_000,
            selected_width=None,
            selected_height=None,
            selected_fps=None,
            selected_bitrate_bps=None,
            expires_at=datetime.now(),
        )

        status = main.organization_stream_status([], [waiting_request])

        self.assertTrue(status["awaiting_approval"])

    def test_organization_stream_page_renews_each_visible_live_tablet(self):
        streams = [
            SimpleNamespace(device_credential_id="tablet-b", media_kind="live"),
            SimpleNamespace(device_credential_id="tablet-a", media_kind="live"),
            SimpleNamespace(device_credential_id="tablet-a", media_kind="live"),
            SimpleNamespace(device_credential_id="tablet-c", media_kind="recording"),
        ]

        self.assertEqual(
            ("tablet-a", "tablet-b"),
            main.thumbnail_preview_device_ids(streams),
        )

    def test_tablet_stream_page_limits_preview_renewal_to_that_tablet(self):
        streams = [
            SimpleNamespace(device_credential_id="tablet-a", media_kind="live"),
            SimpleNamespace(device_credential_id="tablet-b", media_kind="live"),
        ]

        self.assertEqual(
            ("tablet-b",),
            main.thumbnail_preview_device_ids(streams, "tablet-b"),
        )
        self.assertEqual(
            (),
            main.thumbnail_preview_device_ids(streams, "tablet-missing"),
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "control-plane.db"
        self.store = ControlPlaneStore(
            f"sqlite+aiosqlite:///{database_path}"
        )
        asyncio.run(self.store.init())
        flight_database_path = Path(self.temp_dir.name) / "flights.db"
        self.flight_engine = create_async_engine(
            f"sqlite+aiosqlite:///{flight_database_path}"
        )
        self.flight_sessions = async_sessionmaker(
            bind=self.flight_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        asyncio.run(self.create_flight_schema())

        async def test_get_db():
            async with self.flight_sessions() as session:
                yield session

        main.app.dependency_overrides[main.get_db] = test_get_db
        self.tokens = ControlPlaneTokenService(
            "route-test-signing-key-that-is-longer-than-thirty-two-characters",
            "https://r2c-tracker.com",
        )
        self.patches = ExitStack()
        self.patches.enter_context(
            patch.object(main, "control_plane_store", self.store)
        )
        self.patches.enter_context(
            patch.object(main, "control_plane_tokens", self.tokens)
        )
        self.patches.enter_context(
            patch.object(
                main,
                "platform_admin_identity_provider",
                StaticPlatformAdminIdentityProvider(),
            )
        )
        self.patches.enter_context(patch.object(main, "CONTROL_PLANE_SIMULATION", True))
        self.patches.enter_context(patch.object(main, "SECRET_KEY", "route-test-secret"))
        asyncio.run(
            self.store.ensure_platform_admin(
                email="platform@example.test",
                display_name="Platform Administrator",
                bootstrap_password="platform secret phrase",
            )
        )
        self.client = TestClient(main.app)
        login_page = self.client.get("/platform-admin/login")
        login = self.client.post(
            "/platform-admin/login",
            data={
                "form_token": self.form_token(login_page),
                "email": "platform@example.test",
                "password": "platform secret phrase",
                "next": "/platform-admin/organizations",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.pop(main.get_db, None)
        self.patches.close()
        asyncio.run(self.store.dispose())
        asyncio.run(self.flight_engine.dispose())
        self.temp_dir.cleanup()

    async def create_flight_schema(self):
        async with self.flight_engine.begin() as connection:
            await connection.run_sync(main.Base.metadata.create_all)

    async def add_flight(self, organization_id, sar_id):
        async with self.flight_sessions() as session:
            flight = main.Flight(
                organization_id=organization_id,
                sar_id=sar_id,
                uas="m3t",
                start_time=datetime(2026, 7, 1, 12, 0),
                end_time=datetime(2026, 7, 1, 12, 10),
                start_lat=39.0,
                start_lng=-121.0,
                hours=1 / 6,
                distance_mi=1.0,
            )
            session.add(flight)
            await session.commit()
            return flight.id

    async def all_flights(self):
        async with self.flight_sessions() as session:
            result = await session.execute(select(main.Flight).order_by(main.Flight.id))
            return result.scalars().all()

    @staticmethod
    def form_token(response):
        match = HIDDEN_TOKEN_RE.search(response.text)
        if match is None:
            raise AssertionError("Form token not found")
        return html.unescape(match.group(1))

    @staticmethod
    def platform_form_token(response):
        match = PLATFORM_FORM_TOKEN_RE.search(response.text)
        if match is None:
            raise AssertionError("Platform organization form token not found")
        return html.unescape(match.group(1))

    def test_organization_pages_show_guest_or_authenticated_member(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        guest_page = self.client.get("/ncssar/login")
        self.assertEqual(200, guest_page.status_code)
        self.assertIn("User: Guest", guest_page.text)
        self.assertIn('href="/ncssar/admin"', guest_page.text)
        self.assertNotIn('href="/admin"', guest_page.text)
        self.assertIn('class="site-home" href="/"', guest_page.text)
        self.assertNotIn('href="/r2c"', guest_page.text)
        self.assertNotIn('href="/docs"', guest_page.text)

        directory_page = self.client.get("/")
        self.assertIn("User: Guest", directory_page.text)
        self.assertIn('action="/login"', directory_page.text)
        self.assertIn('name="organization"', directory_page.text)
        self.assertNotIn("Community-supported.", directory_page.text)
        self.assertNotIn('href="https://rid2caltopo.com/donations"', directory_page.text)
        self.assertNotIn("Support the project", directory_page.text)
        self.assertNotIn('href="https://paypal.me/kjtgv"', directory_page.text)
        self.assertNotIn("tax-deductible", directory_page.text.lower())
        self.assertGreaterEqual(
            directory_page.text.count(
                'href="https://rid2caltopo.org/managed-pilot"'
            ),
            2,
        )
        self.assertIn("Request access", directory_page.text)
        self.assertNotIn('href="/r2c"', directory_page.text)
        self.assertNotIn('href="/admin"', directory_page.text)
        self.assertNotIn('href="/flightlogs/list"', directory_page.text)
        self.assertNotIn('href="/export"', directory_page.text)
        self.assertNotIn('href="/docs"', directory_page.text)

        versions_page = self.client.get("/versions")
        self.assertNotIn('href="/r2c"', versions_page.text)
        self.assertNotIn('href="/admin"', versions_page.text)

        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )
        login_page = self.client.get("/ncssar/login")
        login = self.client.post(
            "/ncssar/login",
            data={
                "form_token": self.form_token(login_page),
                "email": organization.primary_admin_email,
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)

        authenticated_login_page = self.client.get("/ncssar/login")
        self.assertIn(
            "User: Primary Administrator",
            authenticated_login_page.text,
        )
        admin_page = self.client.get("/ncssar/admin")
        self.assertIn("User: Primary Administrator", admin_page.text)
        self.assertIn('class="site-home" href="/"', admin_page.text)
        self.assertIn('href="/ncssar/admin"', admin_page.text)
        self.assertIn('href="/ncssar/admin/flights"', admin_page.text)
        public_dashboard = self.client.get("/ncssar")
        self.assertIn("User: Primary Administrator", public_dashboard.text)
        authenticated_directory = self.client.get("/")
        self.assertIn("User: Primary Administrator", authenticated_directory.text)
        self.assertIn(
            "You are signed in as <strong>Primary Administrator</strong>",
            authenticated_directory.text,
        )
        self.assertIn('action="/organizations/select"', authenticated_directory.text)
        self.assertIn('value="NCSSAR"', authenticated_directory.text)
        self.assertIn(
            'src="/static/organization_directory.js?v=20260812-1"',
            authenticated_directory.text,
        )
        self.assertNotIn("picker.showModal()", authenticated_directory.text)
        directory_script = self.client.get("/static/organization_directory.js")
        self.assertEqual(200, directory_script.status_code)
        self.assertIn("picker.showModal()", directory_script.text)
        billing_snapshot = SimpleNamespace(
            source_status="ready",
            source_message="Live billing data.",
            billing_period="2026-08",
            billing_data_through=datetime(2026, 8, 10, 12, 0),
            actual_cost_breakdown_mtd=main.CostBreakdown(
                compute=Decimal("0.08"),
                storage=Decimal("0.25"),
                database=Decimal("1.07"),
            ),
        )
        with patch.object(
            main,
            "load_platform_billing_snapshot",
            return_value=billing_snapshot,
        ):
            billing_report = self.client.get("/ncssar/admin")
        self.assertIn("Month-to-date platform cost", billing_report.text)
        self.assertIn(">$1.40<", billing_report.text)
        self.assertIn("<td>Compute</td><td>$0.08</td>", billing_report.text)
        self.assertIn("<td>Storage</td><td>$0.25</td>", billing_report.text)
        self.assertIn("<td>Database</td><td>$1.07</td>", billing_report.text)
        self.assertIn("shadow allocation for transparency", billing_report.text)
        fake_checkout = SimpleNamespace(
            is_configured=True,
            create_checkout=Mock(
                return_value="https://checkout.stripe.test/session"
            ),
        )
        with patch.object(main, "stripe_checkout_provider", fake_checkout):
            billing_page = self.client.get("/ncssar/admin")
            self.assertIn("Continue to Stripe", billing_page.text)
            checkout = self.client.post(
                "/ncssar/billing/checkout",
                data={
                    "form_token": self.form_token(billing_page),
                    "amount": "25.00",
                },
                follow_redirects=False,
            )
        self.assertEqual(303, checkout.status_code)
        self.assertEqual(
            "https://checkout.stripe.test/session",
            checkout.headers["location"],
        )
        self.assertEqual(
            Decimal("25.00"),
            fake_checkout.create_checkout.call_args.kwargs["amount"],
        )

    def test_records_viewer_lands_on_dashboard_and_directory_shows_identity(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )
        viewer = asyncio.run(
            self.store.add_user(
                organization_id=organization.id,
                display_name="Records Viewer",
                email="viewer@ncssar.example",
                roles=("records_viewer",),
                actor_id=owner.id,
            )
        )
        invitation = asyncio.run(
            self.store.get_invitation(
                organization.designator,
                viewer.email,
            )
        )
        activation_url = self.tokens.activation_url(invitation)
        activation_path = urlparse(activation_url).path + "?" + urlparse(
            activation_url
        ).query
        activation_page = self.client.get(activation_path)
        activated = self.client.post(
            "/ncssar/activate",
            data={
                "form_token": self.form_token(activation_page),
                "token": parse_qs(urlparse(activation_url).query)["token"][0],
                "password": "correct horse battery staple",
                "password_confirm": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, activated.status_code)
        self.assertEqual("/ncssar", activated.headers["location"])

        dashboard = self.client.get(activated.headers["location"])
        self.assertEqual(200, dashboard.status_code)
        self.assertIn("User: Records Viewer", dashboard.text)
        self.assertEqual(403, self.client.get("/ncssar/admin").status_code)

        directory = self.client.get("/")
        self.assertIn("User: Records Viewer", directory.text)
        self.assertIn(
            "You are signed in as <strong>Records Viewer</strong>",
            directory.text,
        )
        selected = self.client.post(
            "/organizations/select",
            data={
                "form_token": self.form_token(directory),
                "designator": "NCSSAR",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, selected.status_code)
        self.assertEqual("/ncssar", selected.headers["location"])

        self.client.cookies.clear()
        guest_dashboard = self.client.get("/ncssar", follow_redirects=False)
        self.assertEqual(303, guest_dashboard.status_code)
        self.assertEqual(
            "/ncssar/login?next=%2Fncssar",
            guest_dashboard.headers["location"],
        )
        generic_login = self.client.get(
            "/login?organization=ncssar",
            follow_redirects=False,
        )
        self.assertEqual(303, generic_login.status_code)
        self.assertEqual("/ncssar/login", generic_login.headers["location"])

    def test_google_identity_can_switch_memberships_but_password_identity_cannot(self):
        first = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Ken Taylor",
                admin_email="ken@example.test",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        second = asyncio.run(
            self.store.create_organization(
                legal_name="Hill County Search and Rescue",
                designator="HCSAR",
                admin_name="Ken Taylor",
                admin_email="ken@example.test",
                postal_address="200 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        for organization in (first, second):
            asyncio.run(
                self.store.activate_owner(
                    organization.designator,
                    organization.primary_admin_email,
                    "correct horse battery staple",
                )
            )

        with patch.object(
            main,
            "google_oidc_client",
            FakeGoogleOidcClient(first.primary_admin_email),
        ):
            self.client.get("/ncssar/google/start", follow_redirects=False)
            login = self.client.get(
                "/google/callback?code=test-code&state=organization-state",
                follow_redirects=False,
            )
        self.assertEqual(303, login.status_code)

        directory = self.client.get("/")
        self.assertIn("North County Search and Rescue", directory.text)
        self.assertIn("Hill County Search and Rescue", directory.text)
        selected = self.client.post(
            "/organizations/select",
            data={
                "form_token": self.form_token(directory),
                "designator": "HCSAR",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, selected.status_code)
        self.assertEqual("/hcsar/admin", selected.headers["location"])
        hcsar_admin = self.client.get(selected.headers["location"])
        self.assertEqual(200, hcsar_admin.status_code)
        self.assertIn("User: Ken Taylor", hcsar_admin.text)

        self.client.cookies.clear()
        password_login_page = self.client.get("/ncssar/login")
        password_login = self.client.post(
            "/ncssar/login",
            data={
                "form_token": self.form_token(password_login_page),
                "email": first.primary_admin_email,
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, password_login.status_code)
        password_directory = self.client.get("/")
        self.assertNotIn("Hill County Search and Rescue", password_directory.text)
        rejected = self.client.post(
            "/organizations/select",
            data={
                "form_token": self.form_token(password_directory),
                "designator": "HCSAR",
            },
            follow_redirects=False,
        )
        self.assertEqual(403, rejected.status_code)

    def test_owner_can_edit_delete_and_restore_member_from_admin_panel(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )
        member = asyncio.run(
            self.store.add_user(
                organization_id=organization.id,
                display_name="Member To Edit",
                email="member@ncssar.example",
                roles=("records_viewer",),
                actor_id=owner.id,
            )
        )
        login_page = self.client.get("/ncssar/login")
        login = self.client.post(
            "/ncssar/login",
            data={
                "form_token": self.form_token(login_page),
                "email": organization.primary_admin_email,
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)

        page = self.client.get("/ncssar/admin")
        self.assertIn(
            f'class="member-link" href="#member-{member.id}"',
            page.text,
        )
        self.assertIn("Save member", page.text)
        updated = self.client.post(
            f"/ncssar/members/{member.id}",
            data={
                "form_token": self.form_token(page),
                "display_name": "Edited Member",
                "email": "edited@ncssar.example",
                "roles": ["records_admin", "video_requester"],
            },
            follow_redirects=True,
        )
        self.assertIn("Updated member edited@ncssar.example", updated.text)
        current = asyncio.run(self.store.get_user(member.id))
        self.assertEqual("Edited Member", current.display_name)
        self.assertEqual({"records_admin", "video_requester"}, set(current.roles))

        deleted = self.client.post(
            f"/ncssar/members/{member.id}/delete",
            data={
                "form_token": self.form_token(updated),
                "confirmation": "delete",
            },
            follow_redirects=True,
        )
        self.assertIn("Deleted member edited@ncssar.example", deleted.text)
        self.assertIn("Deleted", deleted.text)
        self.assertIn("Restore as pending member", deleted.text)
        self.assertEqual("disabled", asyncio.run(self.store.get_user(member.id)).state)

        restored = self.client.post(
            f"/ncssar/members/{member.id}/restore",
            data={"form_token": self.form_token(deleted)},
            follow_redirects=True,
        )
        self.assertIn("Restored edited@ncssar.example as a pending member", restored.text)
        self.assertIn("Pending activation", restored.text)
        self.assertEqual("invited", asyncio.run(self.store.get_user(member.id)).state)

    def test_owner_can_restore_archived_member_and_send_activation_email(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )
        member = asyncio.run(
            self.store.add_user(
                organization_id=organization.id,
                display_name="Archived Viewer",
                email="archived@ncssar.example",
                roles=("records_admin", "records_viewer", "video_requester"),
                actor_id=owner.id,
            )
        )

        async def archive_member():
            async with self.store.sessions() as session:
                stored = await session.get(OrganizationUser, member.id)
                stored.state = "archived"
                await session.commit()

        asyncio.run(archive_member())
        login_page = self.client.get("/ncssar/login")
        login = self.client.post(
            "/ncssar/login",
            data={
                "form_token": self.form_token(login_page),
                "email": organization.primary_admin_email,
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)

        page = self.client.get("/ncssar/admin")
        self.assertIn("Archived (inactive)", page.text)
        self.assertIn("Restore archived member", page.text)
        sender = FakeOrganizationEmailSender()
        with (
            patch.object(main, "CONTROL_PLANE_SIMULATION", False),
            patch.object(main, "SESSION_COOKIE_HTTPS_ONLY", True),
            patch.object(main, "platform_admin_email_sender", sender),
        ):
            added = self.client.post(
                "/ncssar/members",
                data={
                    "form_token": self.form_token(page),
                    "display_name": "New Viewer",
                    "email": "new-viewer@ncssar.example",
                    "roles": ["records_viewer"],
                },
                follow_redirects=True,
            )
            pending_invitation = asyncio.run(
                self.store.get_invitation(
                    "NCSSAR",
                    "new-viewer@ncssar.example",
                )
            )
            resent = self.client.post(
                f"/ncssar/members/{pending_invitation.user_id}/invitation",
                data={"form_token": self.form_token(added)},
                follow_redirects=True,
            )
            restored = self.client.post(
                f"/ncssar/members/{member.id}/restore",
                data={"form_token": self.form_token(resent)},
                follow_redirects=True,
            )

        self.assertIn("Added pending member new-viewer@ncssar.example", added.text)
        self.assertIn("A seven-day activation invitation was emailed", added.text)
        self.assertIn("Send invitation", added.text)
        self.assertIn(
            "Sent a fresh seven-day activation invitation to new-viewer@ncssar.example",
            resent.text,
        )
        self.assertIn("A seven-day activation invitation was emailed", restored.text)
        current = asyncio.run(self.store.get_user(member.id))
        self.assertEqual("invited", current.state)
        self.assertEqual(set(member.roles), set(current.roles))
        self.assertEqual(3, len(sender.member_activation_messages))
        self.assertEqual(
            "new-viewer@ncssar.example",
            sender.member_activation_messages[0]["recipient"],
        )
        self.assertEqual(
            "new-viewer@ncssar.example",
            sender.member_activation_messages[1]["recipient"],
        )
        message = sender.member_activation_messages[2]
        self.assertEqual("archived@ncssar.example", message["recipient"])
        self.assertEqual("Archived Viewer", message["member_name"])
        self.assertIn("/ncssar/activate?token=", message["activation_url"])

    def test_platform_navigation_does_not_expose_legacy_admin_links(self):
        self.client.cookies.clear()
        page = self.client.get("/platform-admin/login")
        self.assertEqual(200, page.status_code)
        self.assertIn(
            'class="site-home" href="/platform-admin/organizations"',
            page.text,
        )
        self.assertIn('href="/platform-admin/account"', page.text)
        self.assertNotIn('href="/r2c"', page.text)
        self.assertNotIn('href="/admin"', page.text)
        self.assertNotIn('href="/flightlogs/list"', page.text)
        self.assertNotIn('href="/export"', page.text)
        self.assertNotIn('href="/docs"', page.text)

        retired_admin = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(303, retired_admin.status_code)
        self.assertEqual(
            "/platform-admin/login?next=%2Fplatform-admin%2Forganizations",
            retired_admin.headers["location"],
        )

    def test_records_admin_is_tenant_scoped_and_imports_namespaced_archive(self):
        ncssar = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        exsar = asyncio.run(
            self.store.create_organization(
                legal_name="Example Search and Rescue",
                designator="EXSAR",
                admin_name="Other Administrator",
                admin_email="admin@exsar.example",
                postal_address="200 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        asyncio.run(
            self.store.activate_owner(
                ncssar.designator,
                ncssar.primary_admin_email,
                "correct horse battery staple",
            )
        )
        login_page = self.client.get("/ncssar/login")
        login = self.client.post(
            "/ncssar/login",
            data={
                "form_token": self.form_token(login_page),
                "email": ncssar.primary_admin_email,
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)

        ncssar_flight_id = asyncio.run(self.add_flight(ncssar.id, "NCSSAR-FLIGHT"))
        exsar_flight_id = asyncio.run(self.add_flight(exsar.id, "EXSAR-FLIGHT"))
        legacy_flight_id = asyncio.run(self.add_flight(None, "LEGACY-FLIGHT"))

        page = self.client.get("/ncssar/admin/flights")
        self.assertEqual(200, page.status_code)
        self.assertIn("NCSSAR-FLIGHT", page.text)
        self.assertNotIn("EXSAR-FLIGHT", page.text)
        self.assertNotIn("LEGACY-FLIGHT", page.text)
        self.assertIn('action="/ncssar/admin/flights/import-archive"', page.text)
        self.assertIn('href="/ncssar/admin/flights/export"', page.text)
        self.assertIn('href="/ncssar/admin/flights/archive"', page.text)

        exported = self.client.get("/ncssar/admin/flights/export")
        self.assertEqual(200, exported.status_code)
        self.assertIn("NCSSAR-FLIGHT", exported.text)
        self.assertNotIn("EXSAR-FLIGHT", exported.text)
        self.assertNotIn("LEGACY-FLIGHT", exported.text)

        token = self.form_token(page)
        missing_csrf = self.client.post(
            "/ncssar/admin/flights/delete",
            data={},
            follow_redirects=False,
        )
        self.assertEqual(403, missing_csrf.status_code)
        deleted = self.client.post(
            "/ncssar/admin/flights/batch",
            data={
                "form_token": token,
                "action": "delete_selected",
                "flight_ids": [str(ncssar_flight_id), str(exsar_flight_id)],
                "delete_ids": [str(ncssar_flight_id), str(exsar_flight_id)],
                f"sar_id_{ncssar_flight_id}": "NCSSAR-FLIGHT",
                f"uas_{ncssar_flight_id}": "m3t",
                f"sar_id_{exsar_flight_id}": "EXSAR-FLIGHT",
                f"uas_{exsar_flight_id}": "m3t",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, deleted.status_code)
        remaining_ids = {flight.id for flight in asyncio.run(self.all_flights())}
        self.assertNotIn(ncssar_flight_id, remaining_ids)
        self.assertIn(exsar_flight_id, remaining_ids)
        self.assertIn(legacy_flight_id, remaining_ids)

        start_ms = int(datetime(2026, 7, 2, 12, 0).timestamp() * 1000)
        geojson = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"title": "1SAR7m3t_track"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [-121.0, 39.0, 100.0, start_ms],
                        [-120.99, 39.01, 100.0, start_ms + 120_000],
                    ],
                },
            }],
        }
        archive_bytes = io.BytesIO()
        payload = json.dumps(geojson).encode("utf-8")
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            member = tarfile.TarInfo("2026/07/flightlog_1_ncssar.json")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        archive_bytes.seek(0)

        archive_root = Path(self.temp_dir.name) / "flightlogs"
        with patch.object(main, "BASE_LOG_DIRECTORY", str(archive_root)):
            imported = self.client.post(
                "/ncssar/admin/flights/import-archive",
                data={"form_token": token},
                files={
                    "file": (
                        "legacy-flightlogs.tgz",
                        archive_bytes.getvalue(),
                        "application/gzip",
                    )
                },
                follow_redirects=False,
            )

        self.assertEqual(303, imported.status_code)
        flights = asyncio.run(self.all_flights())
        imported_flights = [
            flight for flight in flights if flight.organization_id == ncssar.id
        ]
        self.assertEqual(1, len(imported_flights))
        self.assertTrue(
            imported_flights[0].archive_relpath.startswith(
                "organizations/ncssar/2026/07/"
            )
        )
        imported_flight_id = imported_flights[0].id
        imported_log_url = (
            f"/ncssar/admin/flights/{imported_flight_id}/log"
        )
        imported_page = self.client.get("/ncssar/admin/flights")
        self.assertIn(f'href="{imported_log_url}"', imported_page.text)
        self.assertIn(
            'classList.toggle("has-overflow", hasOverflow)',
            imported_page.text,
        )
        self.assertIn("new ResizeObserver(updateScrollHint)", imported_page.text)
        with patch.object(main, "BASE_LOG_DIRECTORY", str(archive_root)):
            downloaded_log = self.client.get(imported_log_url)
            cross_tenant_log = self.client.get(
                f"/ncssar/admin/flights/{exsar_flight_id}/log"
            )
        self.assertEqual(200, downloaded_log.status_code)
        self.assertEqual("no-store", downloaded_log.headers["cache-control"])
        downloaded_geojson = downloaded_log.json()
        self.assertEqual(geojson["type"], downloaded_geojson["type"])
        self.assertEqual(geojson["features"], downloaded_geojson["features"])
        self.assertEqual(404, cross_tenant_log.status_code)
        self.assertEqual(
            {exsar_flight_id, legacy_flight_id},
            {
                flight.id for flight in flights
                if flight.organization_id != ncssar.id
            },
        )

        exported_after_import = self.client.get(
            "/ncssar/admin/flights/export"
        )
        exported_rows = list(
            csv.DictReader(io.StringIO(exported_after_import.text))
        )
        self.assertEqual(1, len(exported_rows))
        exported_rows[0]["Incident"] = "Restored Incident"
        exported_rows[0]["Temp (F)"] = "72.5"
        backfill_csv = io.StringIO()
        writer = csv.DictWriter(
            backfill_csv,
            fieldnames=list(exported_rows[0].keys()),
        )
        writer.writeheader()
        writer.writerows(exported_rows)
        backfilled = self.client.post(
            "/ncssar/admin/flights/backfill-csv",
            data={"form_token": token},
            files={
                "file": (
                    "legacy-admin.csv",
                    backfill_csv.getvalue().encode("utf-8"),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )
        self.assertEqual(303, backfilled.status_code)
        flights = asyncio.run(self.all_flights())
        restored = next(
            flight for flight in flights if flight.organization_id == ncssar.id
        )
        self.assertEqual("Restored Incident", restored.incident)
        self.assertEqual(72.5, restored.temp_f)

        with patch.object(main, "BASE_LOG_DIRECTORY", str(archive_root)):
            downloaded_archive = self.client.get(
                "/ncssar/admin/flights/archive"
            )
        self.assertEqual(200, downloaded_archive.status_code)
        with tarfile.open(
            fileobj=io.BytesIO(downloaded_archive.content),
            mode="r:gz",
        ) as archive:
            names = archive.getnames()
        self.assertEqual(1, len(names))
        self.assertTrue(names[0].startswith("2026/07/flightlog_"))

    def test_directory_lists_only_public_organizations_and_restricted_organizations_require_login(self):
        zulu = asyncio.run(
            self.store.create_organization(
                legal_name="Zulu County Search and Rescue",
                designator="ZCSAR",
                admin_name="Zulu Administrator",
                admin_email="admin@zulu.example",
                postal_address="200 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        alpha = asyncio.run(
            self.store.create_organization(
                legal_name="Alpha County Search and Rescue",
                designator="ACSAR",
                admin_name="Alpha Administrator",
                admin_email="admin@alpha.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        hidden = asyncio.run(
            self.store.create_organization(
                legal_name="Hidden County Search and Rescue",
                designator="HCSAR",
                admin_name="Hidden Administrator",
                admin_email="admin@hidden.example",
                postal_address="300 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        for organization in (zulu, alpha):
            asyncio.run(
                self.store.update_settings(
                    organization_id=organization.id,
                    records_visibility="public",
                    record_retention_days=730,
                    log_retention_days=30,
                    notification_email=organization.primary_admin_email,
                    actor_id="organization-owner",
                )
            )

        directory = self.client.get("/")
        self.assertEqual(200, directory.status_code)
        self.assertIn("https://www.rid2caltopo.com/tracker", directory.text)
        self.assertIn('href="/acsar"', directory.text)
        self.assertIn('href="/zcsar"', directory.text)
        self.assertNotIn(hidden.legal_name, directory.text)
        self.assertNotIn('href="/hcsar"', directory.text)
        self.assertNotIn("Sign-in required", directory.text)
        self.assertLess(
            directory.text.index(alpha.legal_name),
            directory.text.index(zulu.legal_name),
        )
        with patch.object(
            main,
            "render_public_dashboard",
            new=AsyncMock(return_value=main.HTMLResponse("public dashboard")),
        ):
            self.assertEqual(200, self.client.get("/acsar").status_code)
        restricted = self.client.get("/hcsar", follow_redirects=False)
        self.assertEqual(303, restricted.status_code)
        self.assertEqual(
            "/hcsar/login?next=%2Fhcsar",
            restricted.headers["location"],
        )
        asyncio.run(
            self.store.activate_owner(
                hidden.designator,
                hidden.primary_admin_email,
                "correct horse battery staple",
            )
        )
        login_page = self.client.get(restricted.headers["location"])
        self.assertIn('name="next" value="/hcsar"', login_page.text)
        login = self.client.post(
            "/hcsar/login",
            data={
                "form_token": self.form_token(login_page),
                "email": hidden.primary_admin_email,
                "password": "correct horse battery staple",
                "next": "/hcsar",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)
        self.assertEqual("/hcsar", login.headers["location"])
        with patch.object(
            main,
            "render_public_dashboard",
            new=AsyncMock(return_value=main.HTMLResponse("restricted dashboard")),
        ):
            self.assertEqual(200, self.client.get("/hcsar").status_code)

    def test_platform_and_organization_logins_coexist_and_logout_independently(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )

        organization_login_page = self.client.get("/ncssar/login")
        organization_login = self.client.post(
            "/ncssar/login",
            data={
                "form_token": self.form_token(organization_login_page),
                "email": organization.primary_admin_email,
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, organization_login.status_code)
        self.assertEqual(
            200,
            self.client.get("/platform-admin/organizations").status_code,
        )

        platform_page = self.client.get("/platform-admin/organizations")
        platform_logout = self.client.post(
            "/platform-admin/logout",
            data={"form_token": self.form_token(platform_page)},
            follow_redirects=False,
        )
        self.assertEqual(303, platform_logout.status_code)
        self.assertEqual(200, self.client.get("/ncssar/admin").status_code)

        platform_login_page = self.client.get("/platform-admin/login")
        platform_login = self.client.post(
            "/platform-admin/login",
            data={
                "form_token": self.form_token(platform_login_page),
                "email": "platform@example.test",
                "password": "platform secret phrase",
                "next": "/platform-admin/organizations",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, platform_login.status_code)
        self.assertEqual(200, self.client.get("/ncssar/admin").status_code)

        organization_page = self.client.get("/ncssar/admin")
        organization_logout = self.client.post(
            "/ncssar/logout",
            data={"form_token": self.form_token(organization_page)},
            follow_redirects=False,
        )
        self.assertEqual(303, organization_logout.status_code)
        self.assertEqual(
            200,
            self.client.get("/platform-admin/organizations").status_code,
        )

    def test_signed_out_organization_page_redirects_to_login(self):
        asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )

        response = self.client.get(
            "/ncssar/streams",
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/ncssar/login", response.headers["location"])

    def test_onboarding_activation_and_enrollment_qr_flow(self):
        platform_page = self.client.get(
            "/platform-admin/organizations",
        )
        self.assertEqual(200, platform_page.status_code)
        self.assertEqual("no-store", platform_page.headers["cache-control"])
        self.assertEqual("no-referrer", platform_page.headers["referrer-policy"])
        self.assertIn(
            "frame-ancestors 'none'",
            platform_page.headers["content-security-policy"],
        )
        self.assertNotIn("unpkg.com", platform_page.text)

        created = self.client.post(
            "/platform-admin/organizations",
            data={
                "form_token": self.platform_form_token(platform_page),
                "legal_name": "North County Search and Rescue",
                "designator": "NCSSAR",
                "admin_name": "Primary Administrator",
                "admin_email": "admin@ncssar.example",
                "postal_address": "100 Rescue Way",
            },
            follow_redirects=True,
        )
        self.assertEqual(200, created.status_code)
        self.assertIn("NCSSAR", created.text)
        self.assertIn("100 Rescue Way", created.text)
        self.assertIn("Provisioning jobs", created.text)
        self.assertIn("Control-plane audit", created.text)
        self.assertIn("organization.created", created.text)
        self.assertNotIn("pilot_acknowledged", created.text)
        self.assertIn("Designators must be unique", created.text)
        activation_match = ACTIVATION_LINK_RE.search(created.text)
        self.assertIsNotNone(activation_match)
        idempotency_match = IDEMPOTENCY_RE.search(created.text)
        self.assertIsNotNone(idempotency_match)
        credited = self.client.post(
            "/platform-admin/organizations/ncssar/credit",
            data={
                "form_token": self.platform_form_token(created),
                "idempotency_key": idempotency_match.group(1),
                "amount": "10.00",
            },
            follow_redirects=True,
        )
        self.assertEqual(200, credited.status_code)
        self.assertIn("Added $10.00 credit", credited.text)
        self.assertIn("r2c-tracker.com/ncssar", credited.text)
        activation_url = html.unescape(activation_match.group(1))
        activation_path = urlparse(activation_url).path + "?" + urlparse(
            activation_url
        ).query

        with patch.object(main, "google_oidc_client", FakeGoogleOidcClient()):
            activation_page = self.client.get(activation_path)
            self.assertEqual(200, activation_page.status_code)
            activation_token = urlparse(activation_url).query.split("token=", 1)[1]
            self.assertIn("or create an R2C Tracker password", activation_page.text)
            oauth_start = self.client.post(
                "/ncssar/activate/google",
                data={
                    "form_token": self.form_token(activation_page),
                    "token": activation_token,
                },
                follow_redirects=False,
            )
            self.assertEqual(303, oauth_start.status_code)

            activated = self.client.get(
                "/google/callback?code=test-code&state=organization-state",
                follow_redirects=True,
            )
        self.assertEqual(200, activated.status_code)
        self.assertIn("NCSSAR administration", activated.text)
        self.assertIn("Credit balance:", activated.text)
        self.assertIn("$10.00", activated.text)
        self.assertIn("Simulation prepaid account credit", activated.text)

        campaign_created = self.client.post(
            "/ncssar/enrollments",
            data={
                "form_token": self.form_token(activated),
                "label": "Drone team training",
                "expires_in_hours": "168",
                "max_redemptions": "25",
            },
            follow_redirects=True,
        )
        self.assertEqual(200, campaign_created.status_code)
        self.assertIn("Drone team training", campaign_created.text)
        qr_match = QR_SRC_RE.search(campaign_created.text)
        self.assertIsNotNone(qr_match)

        qr_response = self.client.get(html.unescape(qr_match.group(1)))
        self.assertEqual(200, qr_response.status_code)
        self.assertEqual("image/svg+xml", qr_response.headers["content-type"])
        self.assertIn(b"<svg", qr_response.content)
        self.assertEqual("no-store", qr_response.headers["cache-control"])
        self.assertIn(
            "NCSSAR-enrollment-",
            qr_response.headers["content-disposition"],
        )
        organization = asyncio.run(self.store.get_organization("NCSSAR"))
        campaign = asyncio.run(
            self.store.list_enrollment_campaigns(organization.id)
        )[0]
        enrollment_token = self.tokens.enrollment_token(
            organization,
            campaign,
        )
        with patch.object(main, "DEVICE_CREDENTIAL_ISSUANCE_ENABLED", True):
            redeemed = self.client.post(
                "/api/v1/device-enrollment/redeem",
                json={
                    "token": enrollment_token,
                    "device_name": "Android field tablet",
                    "platform": "android",
                },
            )
        self.assertEqual(200, redeemed.status_code)
        self.assertEqual("no-store", redeemed.headers["cache-control"])
        self.assertEqual(
            "https://r2c-tracker.com/ncssar",
            redeemed.json()["tracker"]["base_url"],
        )
        installed_token = redeemed.json()["tracker"]["api_key"]
        self.assertTrue(installed_token.startswith("r2c_dev_"))
        self.assertTrue(
            asyncio.run(main.authenticate_tracker_token(installed_token))
        )
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(
                "/othersar/ws/r2c",
                headers={"X-SAR-Token": installed_token},
            ):
                pass

    def test_platform_admin_can_add_credit_in_live_mode(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=False,
            )
        )
        with patch.object(main, "CONTROL_PLANE_SIMULATION", False):
            page = self.client.get("/platform-admin/organizations")
            nonce = IDEMPOTENCY_RE.search(page.text)
            self.assertIsNotNone(nonce)
            credited = self.client.post(
                "/platform-admin/organizations/ncssar/credit",
                data={
                    "form_token": self.platform_form_token(page),
                    "idempotency_key": nonce.group(1),
                    "amount": "20.00",
                },
                follow_redirects=True,
            )

        self.assertEqual(200, credited.status_code)
        self.assertIn("Added $20.00 credit", credited.text)
        funded = asyncio.run(self.store.get_organization("NCSSAR"))
        self.assertEqual(organization.id, funded.id)
        self.assertEqual("funded", funded.lifecycle_state)
        self.assertEqual(Decimal("20.0000"), funded.credit_balance)

    def test_platform_admin_archive_requires_designator_and_disables_site(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        page = self.client.get("/platform-admin/organizations")
        self.assertIn("Archive organization", page.text)

        rejected = self.client.post(
            "/platform-admin/organizations/ncssar/archive",
            data={
                "form_token": self.platform_form_token(page),
                "confirmation": "WRONG",
            },
            follow_redirects=True,
        )
        self.assertIn("Type NCSSAR to confirm", rejected.text)
        self.assertIsNotNone(
            asyncio.run(self.store.get_organization(organization.designator))
        )

        contact_rejected = self.client.post(
            "/platform-admin/organizations/ncssar/archive",
            data={
                "form_token": self.platform_form_token(rejected),
                "confirmation": "NCSSAR",
            },
            follow_redirects=True,
        )
        self.assertIn("Confirm direct contact", contact_rejected.text)

        archived = self.client.post(
            "/platform-admin/organizations/ncssar/archive",
            data={
                "form_token": self.platform_form_token(contact_rejected),
                "confirmation": "NCSSAR",
                "contact_confirmed": "yes",
                "administrator_contact": (
                    "Called Primary Administrator on 06 Aug 2026; confirmed export."
                ),
            },
            follow_redirects=True,
        )
        self.assertIn("NCSSAR archived", archived.text)
        self.assertIn("designator reserved", archived.text)
        self.assertIn("Unarchive organization", archived.text)
        self.assertEqual(404, self.client.get("/ncssar/login").status_code)

        sender = FakeOrganizationEmailSender()
        with patch.object(main, "platform_admin_email_sender", sender):
            restored = self.client.post(
                "/platform-admin/organizations/ncssar/unarchive",
                data={"form_token": self.platform_form_token(archived)},
                follow_redirects=True,
            )
        self.assertIn("NCSSAR unarchived", restored.text)
        self.assertEqual(1, len(sender.activation_messages))
        self.assertEqual(200, self.client.get("/ncssar/login").status_code)

    def test_managed_request_is_authenticated_stored_and_shown_to_platform_admin(self):
        request_data = {
            "requester_name": "Jamie Responder",
            "requester_email": "jamie@example.org",
            "requester_phone": "+1 530 555 0100",
            "organization_name": "Foothill Search and Rescue",
            "designator": "FHSAR",
            "source_host": "rid2caltopo.org",
            "terms_acknowledged": "yes",
            "terms_version": MANAGED_ACCESS_TERMS_VERSION,
        }
        with patch.object(main, "MANAGED_REQUEST_INGEST_KEY", "intake-secret"):
            denied = self.client.post(
                "/managed-access-requests",
                data=request_data,
            )
            missing_acknowledgement = self.client.post(
                "/managed-access-requests",
                data={
                    key: value
                    for key, value in request_data.items()
                    if key not in {"terms_acknowledged", "terms_version"}
                },
                headers={"Authorization": "Bearer intake-secret"},
            )
            accepted = self.client.post(
                "/managed-access-requests",
                data=request_data,
                headers={"Authorization": "Bearer intake-secret"},
            )
        self.assertEqual(403, denied.status_code)
        self.assertEqual(422, missing_acknowledgement.status_code)
        self.assertEqual(200, accepted.status_code)
        page = self.client.get("/platform-admin/organizations")
        self.assertIn("Managed pilot requests", page.text)
        self.assertIn("Jamie Responder", page.text)
        self.assertIn("+1 530 555 0100", page.text)
        self.assertIn("jamie@example.org", page.text)
        self.assertIn("Acknowledged", page.text)
        self.assertIn(MANAGED_ACCESS_TERMS_VERSION, page.text)

    def test_platform_admin_can_replace_organization_administrator_with_accountability_email(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Former Administrator",
                admin_email="former@ncssar.example",
                admin_phone="530-555-0101",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=False,
            )
        )
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )
        sender = FakeOrganizationEmailSender()
        with patch.object(main, "platform_admin_email_sender", sender):
            page = self.client.get("/platform-admin/organizations")
            changed = self.client.post(
                "/platform-admin/organizations/ncssar/contact",
                data={
                    "form_token": self.platform_form_token(page),
                    "legal_name": "Nevada County Sheriff's Search And Rescue",
                    "admin_name": "New Administrator",
                    "admin_email": "new@ncssar.example",
                    "admin_phone": "+1 530 555 0199",
                    "postal_address": "200 New Rescue Way",
                },
                follow_redirects=True,
            )
        self.assertIn("administrator replaced", changed.text)
        self.assertIn("+1 530 555 0199", changed.text)
        self.assertEqual(1, len(sender.activation_messages))
        self.assertEqual(1, len(sender.administrator_change_messages))
        self.assertEqual(
            "former@ncssar.example",
            sender.administrator_change_messages[0]["recipient"],
        )
        self.assertIsNone(
            asyncio.run(
                self.store.authenticate_user(
                    "NCSSAR",
                    "former@ncssar.example",
                    "correct horse battery staple",
                )
            )
        )
        self.assertIsNotNone(
            asyncio.run(
                self.store.get_invitation("NCSSAR", "new@ncssar.example")
            )
        )

    def test_active_organization_receives_access_email_instead_of_new_activation(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=False,
            )
        )
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )
        sender = FakeOrganizationEmailSender()
        with (
            patch.object(main, "CONTROL_PLANE_SIMULATION", False),
            patch.object(main, "platform_admin_email_sender", sender),
        ):
            page = self.client.get("/platform-admin/organizations")
            self.assertIn("Send access email", page.text)
            sent = self.client.post(
                "/platform-admin/organizations/ncssar/send-invitation",
                data={"form_token": self.platform_form_token(page)},
                follow_redirects=True,
            )

        self.assertIn("Administrator access email sent", sent.text)
        self.assertEqual([], sender.activation_messages)
        self.assertEqual(1, len(sender.access_messages))
        self.assertEqual(
            "https://r2c-tracker.com/ncssar/login",
            sender.access_messages[0]["login_url"],
        )
        audit_events = asyncio.run(self.store.list_audit_events())
        self.assertEqual(
            "administrator.access_email_sent",
            audit_events[0].event_type,
        )

    def test_active_organization_user_can_sign_in_with_google(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )

        with patch.object(main, "google_oidc_client", FakeGoogleOidcClient()):
            login_page = self.client.get("/ncssar/login")
            self.assertIn("Continue with Google", login_page.text)
            self.assertIn('name="password"', login_page.text)
            self.assertIn("Forgot password?", login_page.text)
            start = self.client.get(
                "/ncssar/google/start",
                follow_redirects=False,
            )
            self.assertEqual(303, start.status_code)
            self.assertTrue(
                start.headers["location"].startswith(
                    "https://accounts.google.test/auth"
                )
            )
            callback = self.client.get(
                "/google/callback"
                "?code=test-code&state=organization-state",
                follow_redirects=False,
            )

        self.assertEqual(303, callback.status_code)
        self.assertEqual(
            "/ncssar/admin",
            callback.headers["location"],
        )
        self.assertEqual(
            200,
            self.client.get("/ncssar/admin").status_code,
        )

    def test_video_requester_sees_sorted_streams_and_request_stays_pending(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )
        campaign = asyncio.run(
            self.store.create_enrollment_campaign(
                organization_id=organization.id,
                label="Video tablet",
                created_by_user_id=owner.id,
                expires_in_hours=24,
                max_redemptions=1,
            )
        )
        device = asyncio.run(
            self.store.issue_device_credential(
                campaign_id=campaign.id,
                organization_id=organization.id,
                device_name="Android video tablet",
                platform="android",
            )
        )
        with self.client.websocket_connect(
            "/ncssar/ws/r2c",
            headers={"X-SAR-Token": device.token},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "video_stream_advertisement",
                    "incidentName": "Alpha",
                    "timeZone": "America/Los_Angeles",
                    "streams": [
                        {
                            "sessionId": "00000000-0000-0000-0000-000000000001",
                            "droneDesignator": "10A",
                            "sourceWidth": 1920,
                            "sourceHeight": 1080,
                            "sourceFps": 30,
                            "sourceBitrateBps": 4_000_000,
                            "sourceCodec": "h264",
                        },
                        {
                            "sessionId": "00000000-0000-0000-0000-000000000002",
                            "droneDesignator": "2B",
                            "sourceWidth": 1280,
                            "sourceHeight": 720,
                            "sourceFps": 30,
                            "sourceBitrateBps": 2_000_000,
                            "sourceCodec": "h264",
                            "mediaKind": "recording",
                            "recordedAt": "2026-08-10T19:30:00Z",
                            "durationMs": 91_000,
                            "thumbnailRevision": "recording-thumb-1",
                            "thumbnailJpegBase64": "/9j/2Q==",
                        },
                    ],
                }
            )
            acknowledgement = websocket.receive_json()
            self.assertEqual(
                "video_stream_advertisement_ack",
                acknowledgement["type"],
            )
            self.assertTrue(acknowledgement["accepted"])
        login_page = self.client.get("/ncssar/login")
        login = self.client.post(
            "/ncssar/login",
            data={
                "form_token": self.form_token(login_page),
                "email": organization.primary_admin_email,
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)

        streams_page = self.client.get("/ncssar/streams")

        self.assertEqual(200, streams_page.status_code)
        self.assertEqual("no-store", streams_page.headers["cache-control"])
        # Match rendered table cells rather than arbitrary substrings. Random
        # CSRF/session values can legitimately contain "2B" and made this
        # ordering assertion intermittent.
        self.assertLess(
            streams_page.text.index("<td>10A</td>"),
            streams_page.text.index("<td>2B</td>"),
        )
        self.assertIn("Request video", streams_page.text)
        self.assertIn("Play recording", streams_page.text)
        self.assertIn("10 Aug 2026 12:30:00 PM PDT", streams_page.text)
        self.assertIn("Android video tablet", streams_page.text)
        self.assertIn("R2C instance", streams_page.text)
        self.assertIn("/static/organization_streams_live.js", streams_page.text)
        self.assertIn('data-membership-revision="', streams_page.text)
        self.assertIn('data-status-url="/ncssar/streams/live-status"', streams_page.text)
        self.assertIn('class="stream-preview-image"', streams_page.text)
        self.assertIn("Viewer preview", streams_page.text)
        self.assertIn("does not start video", streams_page.text)
        self.assertNotIn("/whep", streams_page.text)
        self.assertNotIn("/streams/status", streams_page.text)
        tablet_code = tablet_link_code("ncssar", "Android video tablet")
        self.assertEqual("Bz2DZg", tablet_link_code("ncssar", "Kjt A5 Pro"))
        with self.client.websocket_connect(
            "/ncssar/ws/r2c",
            headers={"X-SAR-Token": device.token},
        ) as live_websocket:
            short_link = self.client.get(
                f"/t/{tablet_code}",
                follow_redirects=False,
            )
            self.assertEqual(303, short_link.status_code)
            self.assertEqual(
                "/ncssar/streams/Android%20video%20tablet",
                short_link.headers["location"],
            )
            tablet_page = self.client.get(
                "/ncssar/streams/Android%20video%20tablet"
            )
            self.assertEqual(200, tablet_page.status_code)
            self.assertIn(
                f'data-device-id="{device.id}"',
                tablet_page.text,
            )
            status_response = self.client.get(
                "/ncssar/streams/live-status",
                params={"device": device.id},
            )
            self.assertEqual(200, status_response.status_code)
            self.assertIn("no-store", status_response.headers["cache-control"])
            status_payload = status_response.json()
            self.assertRegex(
                status_payload["membershipRevision"],
                r"^[0-9a-f]{20}$",
            )
            self.assertTrue(status_payload["streams"])
            self.assertTrue(
                any(
                    item["thumbnailUrl"].endswith("?rev=recording-thumb-1")
                    for item in status_payload["streams"]
                )
            )
            self.assertIn("Android video tablet streams", tablet_page.text)
            self.assertIn("<td>10A</td>", tablet_page.text)
            self.assertIn("<td>2B</td>", tablet_page.text)
            thumbnail = self.client.get(
                f"/r2c-thumbnail/{tablet_code}/"
                "00000000-0000-0000-0000-000000000002.jpg"
            )
            self.assertEqual(200, thumbnail.status_code)
            self.assertEqual("image/jpeg", thumbnail.headers["content-type"])
            self.assertEqual(
                "no-store, max-age=0",
                thumbnail.headers["cache-control"],
            )
            self.assertEqual(b"\xff\xd8\xff\xd9", thumbnail.content)
            stream_code = stream_link_code(
                "ncssar",
                "Android video tablet",
                "2B",
            )
            self.assertEqual("Gv2sGQ", stream_code)
            stream_link = self.client.get(
                f"/s/{stream_code}",
                follow_redirects=False,
            )
            self.assertEqual(303, stream_link.status_code)
            self.assertEqual(
                "/ncssar/streams/Android%20video%20tablet/2B",
                stream_link.headers["location"],
            )
            captured_page = self.client.get(stream_link.headers["location"])
            self.assertEqual(200, captured_page.status_code)
            self.assertIn("<td>2B</td>", captured_page.text)
            self.assertNotIn("<td>10A</td>", captured_page.text)
            recording_code = recording_link_code(
                "ncssar",
                "Android video tablet",
                "00000000-0000-0000-0000-000000000002",
            )
            recording_link = self.client.get(
                f"/v/{recording_code}",
                follow_redirects=False,
            )
            self.assertEqual(303, recording_link.status_code)
            self.assertEqual(
                "/ncssar/streams/Android%20video%20tablet/session/"
                "00000000-0000-0000-0000-000000000002",
                recording_link.headers["location"],
            )
            recording_page = self.client.get(recording_link.headers["location"])
            self.assertEqual(200, recording_page.status_code)
            self.assertIn("<td>2B</td>", recording_page.text)
            self.assertNotIn("<td>10A</td>", recording_page.text)
            live_websocket.send_json(
                {
                    "type": "video_stream_advertisement",
                    "incidentName": "Alpha",
                    "timeZone": "America/Los_Angeles",
                    "remoteControlEnabled": True,
                    "streams": [],
                }
            )
            self.assertTrue(live_websocket.receive_json()["accepted"])
            remote_control_page = self.client.get(
                "/ncssar/streams/Android%20video%20tablet"
            )
            self.assertEqual(200, remote_control_page.status_code)
            self.assertIn(
                "Remote Video Control is enabled on this R2C device",
                remote_control_page.text,
            )
            self.assertNotIn(
                "A request identifies you to the drone team",
                remote_control_page.text,
            )
            connected_tablets_page = self.client.get("/ncssar/streams")
            self.assertIn("Connected R2C tablets", connected_tablets_page.text)
            self.assertIn(
                'href="/ncssar/streams/Android%20video%20tablet"',
                connected_tablets_page.text,
            )
            self.assertIn("Android R2C instance", connected_tablets_page.text)
            live_websocket.send_json(
                {
                    "type": "video_stream_advertisement",
                    "incidentName": "Alpha",
                    "timeZone": "America/Los_Angeles",
                    "remoteControlEnabled": False,
                    "streams": [
                        {
                            "sessionId": "00000000-0000-0000-0000-000000000001",
                            "droneDesignator": "10A",
                            "sourceWidth": 1920,
                            "sourceHeight": 1080,
                            "sourceFps": 30,
                            "sourceBitrateBps": 4_000_000,
                            "sourceCodec": "h264",
                        },
                        {
                            "sessionId": "00000000-0000-0000-0000-000000000002",
                            "droneDesignator": "2B",
                            "sourceWidth": 1280,
                            "sourceHeight": 720,
                            "sourceFps": 30,
                            "sourceBitrateBps": 2_000_000,
                            "sourceCodec": "h264",
                            "mediaKind": "recording",
                            "recordedAt": "2026-08-10T19:30:00Z",
                            "durationMs": 91_000,
                        },
                    ],
                }
            )
            self.assertTrue(live_websocket.receive_json()["accepted"])
        unavailable_link = self.client.get(
            f"/t/{tablet_code}",
            follow_redirects=False,
        )
        self.assertEqual(404, unavailable_link.status_code)
        self.assertEqual(
            404,
            self.client.get(f"/s/{stream_code}", follow_redirects=False).status_code,
        )
        with self.client.websocket_connect(
            "/ncssar/ws/r2c",
            headers={"X-SAR-Token": device.token},
        ) as restarted_websocket:
            restarted_websocket.send_json({
                "type": "video_stream_advertisement",
                "incidentName": "Alpha",
                "timeZone": "America/Los_Angeles",
                "streams": [{
                    "sessionId": "00000000-0000-0000-0000-000000000001",
                    "droneDesignator": "10A",
                    "sourceWidth": 1920,
                    "sourceHeight": 1080,
                    "sourceFps": 30,
                    "sourceBitrateBps": 4_000_000,
                    "sourceCodec": "h264",
                }, {
                    "sessionId": "00000000-0000-0000-0000-000000000002",
                    "droneDesignator": "2B",
                    "sourceWidth": 1280,
                    "sourceHeight": 720,
                    "sourceFps": 30,
                    "sourceBitrateBps": 2_000_000,
                    "sourceCodec": "h264",
                    "mediaKind": "recording",
                    "recordedAt": "2026-08-10T19:30:00Z",
                    "durationMs": 91_000,
                }],
            })
            self.assertTrue(restarted_websocket.receive_json()["accepted"])
            restarted_recording_link = self.client.get(
                f"/v/{recording_code}",
                follow_redirects=False,
            )
            self.assertEqual(303, restarted_recording_link.status_code)
            self.assertEqual(
                recording_link.headers["location"],
                restarted_recording_link.headers["location"],
            )
        with self.client.websocket_connect(
            "/ncssar/streams/events"
        ) as event_websocket:
            self.assertEqual("ready", event_websocket.receive_json()["type"])
            event_websocket.portal.call(
                main.organization_stream_event_hub.broadcast,
                organization.id,
            )
            self.assertEqual(
                "streams_changed",
                event_websocket.receive_json()["type"],
            )
            event_websocket.send_text("unsubscribe")
            self.assertEqual(
                "unsubscribed",
                event_websocket.receive_json()["type"],
            )
        stream_form_token = html.unescape(
            STREAM_REQUEST_TOKEN_RE.search(streams_page.text).group(1)
        )
        request = self.client.post(
            "/ncssar/streams/"
            "00000000-0000-0000-0000-000000000001/request",
            data={
                "form_token": stream_form_token
            },
            follow_redirects=False,
        )
        self.assertEqual(303, request.status_code)

        request_id = parse_qs(
            urlparse(request.headers["location"]).query
        )["preflight"][0]
        # A competing lifecycle reload can omit the redirect query.  The
        # session-carried request must still start the browser preflight.
        request = self.client.get("/ncssar/streams")
        self.assertEqual(200, request.status_code)
        self.assertIn("Video will remain off", request.text)
        self.assertIn("pending", request.text)
        self.assertIn("Request in progress", request.text)
        self.assertIn("request-button\" type=\"button\" disabled", request.text)
        self.assertRegex(request.text, r"\bP(?:S|D)T\b")
        self.assertIn("/static/video_preflight.js", request.text)
        offer_sdp = (
            "v=0\r\n"
            "o=- 1 2 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "t=0 0\r\n"
        )
        offer_response = self.client.post(
            f"/ncssar/streams/requests/{request_id}/preflight/offer",
            json={
                "sdp": offer_sdp,
                "form_token": stream_form_token,
            },
        )
        self.assertEqual(200, offer_response.status_code)
        self.assertTrue(offer_response.json()["accepted"])
        self.assertFalse(offer_response.json()["delivered"])

        with self.client.websocket_connect(
            "/ncssar/ws/r2c",
            headers={"X-SAR-Token": device.token},
        ) as replay_websocket:
            replay_websocket.send_json(
                {
                    "type": "video_stream_advertisement",
                    "incidentName": "Alpha",
                    "streams": [
                        {
                            "sessionId": (
                                "00000000-0000-0000-0000-000000000001"
                            ),
                            "droneDesignator": "10A",
                        }
                    ],
                }
            )
            replay_ack = replay_websocket.receive_json()
            self.assertEqual(
                "video_stream_advertisement_ack",
                replay_ack["type"],
            )
            replayed_request = replay_websocket.receive_json()
            self.assertEqual("video_stream_request", replayed_request["type"])
            self.assertEqual(
                organization.primary_admin_email,
                replayed_request["requesterEmail"],
            )
            self.assertEqual(1920, replayed_request["sourceWidth"])
            self.assertEqual(4_000_000, replayed_request["sourceBitrateBps"])
            self.assertTrue(replayed_request["consentRequired"])
            preflight_offer = replay_websocket.receive_json()
            self.assertEqual("video_preflight_offer", preflight_offer["type"])
            self.assertEqual(request_id, preflight_offer["requestId"])
            self.assertEqual(offer_sdp, preflight_offer["sdp"])
            self.assertEqual(2000, preflight_offer["probeDurationMs"])
            answer_sdp = (
                "v=0\r\n"
                "o=- 2 3 IN IP4 127.0.0.1\r\n"
                "s=-\r\n"
                "t=0 0\r\n"
            )
            replay_websocket.send_json(
                {
                    "type": "video_preflight_answer",
                    "requestId": request_id,
                    "sdp": answer_sdp,
                }
            )
            answer_ack = replay_websocket.receive_json()
            self.assertEqual(
                "video_preflight_answer_ack",
                answer_ack["type"],
            )
            self.assertTrue(answer_ack["accepted"])

            async def store_legacy_apple_answer():
                async with self.store.sessions() as session:
                    exchange = await session.get(
                        VideoPreflightExchange,
                        request_id,
                    )
                    exchange.device_answer_sdp = (
                        answer_sdp + "a=max-message-size:262144"
                    )
                    await session.commit()

            asyncio.run(store_legacy_apple_answer())
            preflight_status = self.client.get(
                f"/ncssar/streams/requests/{request_id}/preflight/status"
            )
            self.assertEqual(200, preflight_status.status_code)
            self.assertEqual(
                answer_sdp,
                preflight_status.json()["answerSdp"],
            )
            replay_websocket.send_json(
                {
                    "type": "video_preflight_result",
                    "requestId": replayed_request["requestId"],
                    "routeKind": "direct",
                    "estimatedUplinkBps": 8_000_000,
                }
            )
            preflight_ack = replay_websocket.receive_json()
            self.assertEqual(
                "video_preflight_result_ack",
                preflight_ack["type"],
            )
            self.assertTrue(preflight_ack["accepted"])
            self.assertEqual("awaiting_approval", preflight_ack["state"])
            preflight_page = self.client.get("/ncssar/streams")
            self.assertIn("Direct", preflight_page.text)
            self.assertIn("8.0", preflight_page.text)
            self.assertIn("0.0 MB transferred", preflight_page.text)
            self.assertIn("Video 0 B received", preflight_page.text)
            self.assertIn("VoIP 0 B to R2C", preflight_page.text)
            self.assertIn("0 B from R2C", preflight_page.text)
            self.assertIn("awaiting_approval", preflight_page.text)
            self.assertIn("Cancel", preflight_page.text)
            cancel_response = self.client.post(
                f"/ncssar/streams/requests/{request_id}/cancel",
                data={"form_token": stream_form_token},
                follow_redirects=True,
            )
            self.assertEqual(200, cancel_response.status_code)
            self.assertIn("tablet was notified", cancel_response.text)
            cancellation = replay_websocket.receive_json()
            self.assertEqual(
                "video_stream_request_cancelled",
                cancellation["type"],
            )
            self.assertEqual(request_id, cancellation["requestId"])

        cancelled_page = self.client.get("/ncssar/streams")
        self.assertIn("cancelled", cancelled_page.text)

        self.assertEqual(
            404,
            self.client.get("/organization/ncssar/streams").status_code,
        )

    def test_video_requester_event_socket_wakes_from_empty_stream_list(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )
        login_page = self.client.get("/ncssar/login")
        login = self.client.post(
            "/ncssar/login",
            data={
                "form_token": self.form_token(login_page),
                "email": organization.primary_admin_email,
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)

        with self.client.websocket_connect(
            "/ncssar/streams/events"
        ) as event_websocket:
            ready = event_websocket.receive_json()
            self.assertEqual("ready", ready["type"])
            self.assertFalse(ready["active"])
            self.assertRegex(ready["revision"], r"^[0-9a-f]{20}$")
            event_websocket.portal.call(
                main.organization_stream_event_hub.broadcast,
                organization.id,
            )
            self.assertEqual(
                "streams_changed",
                event_websocket.receive_json()["type"],
            )
            event_websocket.send_text("unsubscribe")
            self.assertEqual(
                "unsubscribed",
                event_websocket.receive_json()["type"],
            )

    def test_google_email_must_be_active_in_the_organization(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
            )
        )

        with patch.object(
            main,
            "google_oidc_client",
            FakeGoogleOidcClient("other@example.test"),
        ):
            self.client.get(
                "/ncssar/google/start",
                follow_redirects=False,
            )
            callback = self.client.get(
                "/google/callback"
                "?code=test-code&state=organization-state",
                follow_redirects=False,
            )

        self.assertEqual(303, callback.status_code)
        self.assertEqual(
            "/ncssar/login",
            callback.headers["location"],
        )
        protected = self.client.get(
            "/ncssar/admin",
            follow_redirects=False,
        )
        self.assertEqual(303, protected.status_code)
        self.assertEqual("/ncssar/login", protected.headers["location"])

    def test_matching_google_email_activates_pending_member(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        invitation = asyncio.run(
            self.store.get_invitation(
                organization.designator,
                organization.primary_admin_email,
            )
        )
        activation_url = self.tokens.activation_url(invitation)
        activation_path = urlparse(activation_url).path + "?" + urlparse(
            activation_url
        ).query
        with patch.object(
            main,
            "google_oidc_client",
            FakeGoogleOidcClient(organization.primary_admin_email),
        ):
            activation_page = self.client.get(activation_path)
            self.assertIn("Continue with Google", activation_page.text)
            self.assertIn('name="password"', activation_page.text)
            google_start = self.client.post(
                "/ncssar/activate/google",
                data={
                    "form_token": self.form_token(activation_page),
                    "token": parse_qs(urlparse(activation_url).query)["token"][0],
                },
                follow_redirects=False,
            )
            self.assertEqual(303, google_start.status_code)
            self.assertIn(
                "form-action 'self' https://accounts.google.com",
                google_start.headers["content-security-policy"],
            )
            callback = self.client.get(
                "/google/callback"
                "?code=test-code&state=organization-state",
                follow_redirects=False,
            )

        self.assertEqual(303, callback.status_code)
        self.assertEqual(
            "/ncssar/admin",
            callback.headers["location"],
        )
        admin_page = self.client.get("/ncssar/admin")
        self.assertEqual(200, admin_page.status_code)
        self.assertIn("active", admin_page.text)

    def test_pending_member_activates_from_ordinary_google_login(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        with patch.object(
            main,
            "google_oidc_client",
            FakeGoogleOidcClient(organization.primary_admin_email),
        ):
            self.client.get("/ncssar/google/start", follow_redirects=False)
            callback = self.client.get(
                "/google/callback?code=test-code&state=organization-state",
                follow_redirects=False,
            )
        self.assertEqual("/ncssar/admin", callback.headers["location"])
        self.assertEqual(200, self.client.get("/ncssar/admin").status_code)
        members = asyncio.run(self.store.list_users(organization.id))
        self.assertEqual("active", members[0].state)

    def test_forgot_password_is_non_enumerating_and_uses_fragment_token(self):
        organization = asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Primary Administrator",
                admin_email="admin@ncssar.example",
                postal_address="100 Rescue Way",
                actor_id="platform-admin",
                simulation=True,
            )
        )
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "original complex password",
            )
        )
        sender = FakeOrganizationEmailSender()
        with patch.object(main, "platform_admin_email_sender", sender):
            request_page = self.client.get("/ncssar/forgot-password")
            known = self.client.post(
                "/ncssar/forgot-password",
                data={
                    "form_token": self.form_token(request_page),
                    "email": organization.primary_admin_email,
                },
                follow_redirects=True,
            )
            request_page = self.client.get("/ncssar/forgot-password")
            unknown = self.client.post(
                "/ncssar/forgot-password",
                data={
                    "form_token": self.form_token(request_page),
                    "email": "unknown@example.test",
                },
                follow_redirects=True,
            )

        generic = "If that address is registered, a password-reset link has been sent."
        self.assertIn(generic, known.text)
        self.assertIn(generic, unknown.text)
        self.assertEqual(1, len(sender.password_resets))
        reset_url = sender.password_resets[0]["reset_url"]
        self.assertEqual("", urlparse(reset_url).query)
        self.assertIn("token", parse_qs(urlparse(reset_url).fragment))

    def test_cross_tenant_session_is_rejected(self):
        asyncio.run(
            self.store.create_organization(
                legal_name="North County Search and Rescue",
                designator="NCSSAR",
                admin_name="Administrator",
                admin_email="admin@ncssar.example",
                postal_address="",
                actor_id="platform-admin",
            )
        )
        asyncio.run(
            self.store.create_organization(
                legal_name="Example County Search and Rescue",
                designator="EXSAR",
                admin_name="Administrator",
                admin_email="admin@exsar.example",
                postal_address="",
                actor_id="platform-admin",
            )
        )
        invitation = asyncio.run(
            self.store.get_invitation("NCSSAR", "admin@ncssar.example")
        )
        asyncio.run(
            self.store.activate_owner(
                "NCSSAR",
                "admin@ncssar.example",
                "correct horse battery staple",
                activation_nonce=invitation.activation_nonce,
            )
        )
        login_page = self.client.get("/ncssar/login")
        login = self.client.post(
            "/ncssar/login",
            data={
                "form_token": self.form_token(login_page),
                "email": "admin@ncssar.example",
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, login.status_code)

        response = self.client.get(
            "/exsar/admin",
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/exsar/login", response.headers["location"])
        records_response = self.client.get(
            "/exsar/admin/flights",
            follow_redirects=False,
        )
        self.assertEqual(303, records_response.status_code)
        self.assertEqual("/exsar/login", records_response.headers["location"])

        ex_invitation = asyncio.run(
            self.store.get_invitation("EXSAR", "admin@exsar.example")
        )
        ex_owner = asyncio.run(
            self.store.activate_owner(
                "EXSAR",
                "admin@exsar.example",
                "another correct battery staple",
                activation_nonce=ex_invitation.activation_nonce,
            )
        )
        ex_organization = asyncio.run(self.store.get_organization("EXSAR"))
        ex_campaign = asyncio.run(
            self.store.create_enrollment_campaign(
                organization_id=ex_organization.id,
                label="EXSAR private enrollment",
                created_by_user_id=ex_owner.id,
                expires_in_hours=24,
                max_redemptions=5,
            )
        )
        cross_tenant_qr = self.client.get(
            f"/ncssar/enrollments/{ex_campaign.id}/qr.svg"
        )
        self.assertEqual(404, cross_tenant_qr.status_code)

    def test_live_org_site_requires_https_only_session_cookie(self):
        with (
            patch.object(main, "CONTROL_PLANE_SIMULATION", False),
            patch.object(main, "SESSION_COOKIE_HTTPS_ONLY", False),
        ):
            self.assertFalse(main.organization_site_ready())

        with (
            patch.object(main, "CONTROL_PLANE_SIMULATION", False),
            patch.object(main, "SESSION_COOKIE_HTTPS_ONLY", True),
        ):
            self.assertTrue(main.organization_site_ready())


if __name__ == "__main__":
    unittest.main()
