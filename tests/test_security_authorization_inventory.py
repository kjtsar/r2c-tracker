import asyncio
import inspect
import pathlib
import unittest
from unittest.mock import patch

import main


class SecurityAuthorizationInventoryTest(unittest.TestCase):
    """Fail closed when a new tenant or platform route lacks a declared guard."""

    ORGANIZATION_BOOTSTRAP_ENDPOINTS = {
        "organization_activate_page",
        "organization_activate_with_password",
        "organization_activate_with_google",
        "organization_activate_with_microsoft",
        "organization_login_page",
        "organization_login",
        "organization_forgot_password_page",
        "organization_forgot_password_request",
        "organization_reset_password_page",
        "organization_reset_password",
        "organization_google_start",
        "organization_microsoft_start",
        "organization_logout",
        "organization_enrollment_landing",
    }

    ORGANIZATION_SPECIAL_ENDPOINTS = {
        "upload",
        "organization_stream_events",
        "organization_r2c_websocket_endpoint",
        "organization_public_dashboard",
    }

    PLATFORM_BOOTSTRAP_ENDPOINTS = {
        "platform_admin_login_page",
        "platform_admin_login",
        "platform_admin_google_start",
        "platform_admin_setup_request",
        "platform_admin_setup_page",
        "platform_admin_setup_password",
    }

    @staticmethod
    def routes():
        return tuple(
            route
            for route in main.app.routes
            if getattr(route, "endpoint", None) is not None
        )

    def test_every_organization_route_has_a_declared_authorization_policy(self):
        for route in self.routes():
            path = getattr(route, "path", "")
            if "{designator}" not in path or path.startswith("/platform-admin/"):
                continue
            endpoint_name = route.endpoint.__name__
            source = inspect.getsource(route.endpoint)
            guarded = (
                "require_organization_user" in source
                or "require_organization_records_admin" in source
            )
            declared_exception = endpoint_name in (
                self.ORGANIZATION_BOOTSTRAP_ENDPOINTS
                | self.ORGANIZATION_SPECIAL_ENDPOINTS
            )
            self.assertTrue(
                guarded or declared_exception,
                f"{path} ({endpoint_name}) has no declared tenant authorization policy",
            )

    def test_special_organization_routes_enforce_their_scoped_mechanism(self):
        source = inspect.getsource(main.upload)
        self.assertIn("require_scoped_upload_credential", source)

        source = inspect.getsource(main.organization_stream_events)
        for marker in (
            'websocket.session.get("organization_user_id")',
            "user.organization_id != organization.id",
            "session_designator != organization.designator",
            '"video_requester" not in user.roles',
        ):
            self.assertIn(marker, source)

        source = inspect.getsource(main.organization_r2c_websocket_endpoint)
        self.assertIn("serve_r2c_websocket(websocket, designator)", source)
        websocket_source = inspect.getsource(main.serve_r2c_websocket)
        self.assertIn("authenticate_tracker_session", websocket_source)
        self.assertIn("organization_mismatch", websocket_source)

        source = inspect.getsource(main.organization_public_dashboard)
        self.assertIn('records_visibility != "public"', source)
        self.assertIn("require_organization_user", source)

    def test_tenant_browser_mutations_require_csrf(self):
        exempt = {
            "upload",  # Device credential API, not a browser session.
        }
        for route in self.routes():
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set()) or set()
            endpoint_name = route.endpoint.__name__
            if (
                "{designator}" not in path
                or path.startswith("/platform-admin/")
                or not methods.intersection({"POST", "PUT", "PATCH", "DELETE"})
                or endpoint_name in exempt
            ):
                continue
            self.assertIn(
                "verify_csrf",
                inspect.getsource(route.endpoint),
                f"{path} ({endpoint_name}) changes tenant state without CSRF verification",
            )

    def test_platform_routes_are_bootstrap_or_platform_authenticated(self):
        for route in self.routes():
            path = getattr(route, "path", "")
            if not path.startswith("/platform-admin"):
                continue
            endpoint_name = route.endpoint.__name__
            source = inspect.getsource(route.endpoint)
            self.assertTrue(
                endpoint_name in self.PLATFORM_BOOTSTRAP_ENDPOINTS
                or "check_platform_admin" in source,
                f"{path} ({endpoint_name}) lacks platform authentication",
            )

    def test_authenticated_platform_mutations_require_csrf(self):
        for route in self.routes():
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set()) or set()
            source = inspect.getsource(route.endpoint)
            if (
                not path.startswith("/platform-admin")
                or "POST" not in methods
                or "check_platform_admin" not in source
            ):
                continue
            self.assertIn(
                "verify_csrf",
                source,
                f"{path} changes platform state without CSRF verification",
            )

    def test_every_state_changing_route_declares_an_access_mechanism(self):
        browser_bootstrap = (
            self.ORGANIZATION_BOOTSTRAP_ENDPOINTS
            | self.PLATFORM_BOOTSTRAP_ENDPOINTS
        )
        signed_or_vendor_callbacks = {
            "managed_access_request_ingest",
            "redeem_device_enrollment",
        }
        for route in self.routes():
            methods = getattr(route, "methods", set()) or set()
            if not methods.intersection({"POST", "PUT", "PATCH", "DELETE"}):
                continue
            endpoint_name = route.endpoint.__name__
            source = inspect.getsource(route.endpoint)
            has_guard = any(
                marker in source
                for marker in (
                    "check_admin",
                    "check_platform_admin",
                    "require_organization_user",
                    "require_organization_records_admin",
                    "get_api_key",
                    "require_deployment_gate_key",
                )
            )
            self.assertTrue(
                has_guard
                or endpoint_name in browser_bootstrap
                or endpoint_name in signed_or_vendor_callbacks,
                f"{route.path} ({endpoint_name}) changes state without a declared access mechanism",
            )

    def test_container_uses_the_reviewed_dependency_lock(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile").read_text()
        lock = (root / "requirements.lock").read_text()
        self.assertIn("COPY requirements.txt requirements.lock ./", dockerfile)
        self.assertIn("pip install --no-cache-dir -r requirements.lock", dockerfile)
        self.assertIn("fastapi==", lock)
        self.assertIn("sqlalchemy==", lock)
        self.assertNotIn("stripe==", lock)

    def test_security_gate_accepts_python_from_path(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        security_gate = (root / "scripts" / "security_checks.sh").read_text()
        self.assertIn('command -v "${PYTHON}"', security_gate)
        self.assertIn('PYTHON="${PYTHON_PATH}"', security_gate)
        self.assertIn("':!.secrets.baseline'", security_gate)

    def test_missing_organization_device_token_fails_closed(self):
        authenticated, credential = asyncio.run(
            main.authenticate_tracker_session(None)
        )

        self.assertFalse(authenticated)
        self.assertIsNone(credential)

    def test_retired_global_admin_is_disabled_by_default(self):
        self.assertFalse(main.LEGACY_ADMIN_ENABLED)

    def test_browser_cors_is_not_wildcarded(self):
        self.assertNotIn("*", main.CORS_ALLOWED_ORIGINS)

    def test_production_responses_enable_hsts(self):
        with patch.object(main, "SESSION_COOKIE_HTTPS_ONLY", True):
            from fastapi.testclient import TestClient

            response = TestClient(main.app).get("/health")

        self.assertEqual(
            "max-age=31536000; includeSubDomains",
            response.headers["strict-transport-security"],
        )


if __name__ == "__main__":
    unittest.main()
