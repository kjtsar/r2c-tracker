import base64
import hashlib
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

from platform_admin_auth import (
    GMAIL_SEND_SCOPE,
    GmailApiPlatformAdminEmailSender,
    GoogleOidcClient,
    MicrosoftOidcClient,
    PlatformAdminAuthError,
    SmtpPlatformAdminEmailSender,
    _organization_extended_beta_allowance_message,
)


class PlatformAdminAuthHelpersTest(unittest.TestCase):
    def test_extended_beta_video_notice_preserves_non_video_services(self):
        message = _organization_extended_beta_allowance_message(
            "tracker@example.org",
            "billing@ncssar.example",
            "Billing Administrator",
            "North County SAR",
            "NCSSAR",
            "beta_video_disabled",
            "31 Aug 2026",
            "https://r2c-tracker.com/ncssar/admin#service-status",
        )
        body = message.get_content()
        self.assertIn("disabled for the remainder of the month ending 31 Aug 2026", body)
        self.assertIn("Flight logs", body)
        self.assertIn("R2C-based drone-owner arbitration", body)
        self.assertIn("does not accept payments", body)

    def test_microsoft_authorization_uses_organizations_state_nonce_and_pkce(self):
        client = MicrosoftOidcClient("client-id", "client-secret")

        url, flow = client.authorization_request(
            "https://r2c-tracker.com/microsoft/callback"
        )
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(flow["verifier"].encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")

        self.assertEqual("login.microsoftonline.com", parsed.hostname)
        self.assertEqual("/organizations/oauth2/v2.0/authorize", parsed.path)
        self.assertEqual(["openid email profile"], query["scope"])
        self.assertEqual([flow["state"]], query["state"])
        self.assertEqual([flow["nonce"]], query["nonce"])
        self.assertEqual([expected_challenge], query["code_challenge"])
        self.assertEqual(["S256"], query["code_challenge_method"])

    @patch("google.auth.jwt.decode")
    @patch("platform_admin_auth.requests.get")
    @patch("platform_admin_auth.requests.post")
    def test_microsoft_exchange_validates_tenant_issuer_and_identity(
        self, post, get, decode
    ):
        tenant = "12345678-1234-1234-1234-123456789abc"
        issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
        token_response = Mock()
        token_response.json.return_value = {"id_token": "signed-token"}
        token_response.raise_for_status.return_value = None
        post.return_value = token_response
        metadata_response = Mock()
        metadata_response.json.return_value = {
            "issuer": issuer,
            "jwks_uri": (
                f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
            ),
        }
        metadata_response.raise_for_status.return_value = None
        keys_response = Mock()
        keys_response.json.return_value = {
            "keys": [{"kid": "signing-key", "x5c": ["certificate"]}]
        }
        keys_response.raise_for_status.return_value = None
        get.side_effect = [metadata_response, keys_response]
        claims = {
            "aud": "client-id",
            "iss": issuer,
            "tid": tenant,
            "oid": "ABCDEFAB-1234-1234-1234-ABCDEFABCDEF",
            "preferred_username": "Responder@Example.org",
            "name": "SAR Responder",
            "nonce": "expected-nonce",
            "ver": "2.0",
        }
        decode.side_effect = [{"tid": tenant}, claims]

        identity = MicrosoftOidcClient(
            "client-id", "client-secret"
        ).exchange_code(
            code="authorization-code",
            redirect_uri="https://r2c-tracker.com/microsoft/callback",
            verifier="pkce-verifier",
            expected_nonce="expected-nonce",
        )

        self.assertEqual(issuer, identity.issuer)
        self.assertEqual("abcdefab-1234-1234-1234-abcdefabcdef", identity.subject)
        self.assertEqual("responder@example.org", identity.email)
        self.assertEqual("SAR Responder", identity.name)
        verified_call = decode.call_args_list[1]
        self.assertEqual("client-id", verified_call.kwargs["audience"])
        self.assertIn("signing-key", verified_call.kwargs["certs"])

    def test_microsoft_tenant_rejects_consumer_authority(self):
        with self.assertRaises(PlatformAdminAuthError):
            MicrosoftOidcClient("client-id", "client-secret", "common")

    def test_google_authorization_uses_state_nonce_and_pkce(self):
        client = GoogleOidcClient("client-id", "client-secret")

        url, flow = client.authorization_request(
            "https://r2c-tracker.com/platform-admin/google/callback"
        )
        query = parse_qs(urlparse(url).query)
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(flow["verifier"].encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")

        self.assertEqual(["client-id"], query["client_id"])
        self.assertEqual(["openid email profile"], query["scope"])
        self.assertEqual([flow["state"]], query["state"])
        self.assertEqual([flow["nonce"]], query["nonce"])
        self.assertEqual(["S256"], query["code_challenge_method"])
        self.assertEqual([expected_challenge], query["code_challenge"])
        self.assertEqual(["select_account"], query["prompt"])

    def test_gmail_authorization_requests_offline_send_only_access(self):
        client = GoogleOidcClient("client-id", "client-secret")

        url, flow = client.gmail_authorization_request(
            "https://r2c-tracker.com/platform-admin/google/callback"
        )
        query = parse_qs(urlparse(url).query)

        self.assertIn(GMAIL_SEND_SCOPE, query["scope"][0].split())
        self.assertEqual(["offline"], query["access_type"])
        self.assertEqual(["true"], query["include_granted_scopes"])
        self.assertEqual(["consent"], query["prompt"])
        self.assertEqual([flow["state"]], query["state"])

    @patch("google.oauth2.id_token.verify_oauth2_token")
    @patch("platform_admin_auth.requests.post")
    def test_gmail_exchange_requires_refresh_token(self, post, verify_token):
        response = Mock()
        response.json.return_value = {
            "id_token": "signed-token",
            "refresh_token": "offline-refresh-token",
        }
        response.raise_for_status.return_value = None
        post.return_value = response
        verify_token.return_value = {
            "sub": "google-subject",
            "email": "admin@example.org",
            "email_verified": True,
            "nonce": "expected-nonce",
        }
        client = GoogleOidcClient("client-id", "client-secret")

        authorization = client.exchange_gmail_code(
            code="authorization-code",
            redirect_uri="https://r2c-tracker.com/platform-admin/google/callback",
            verifier="pkce-verifier",
            expected_nonce="expected-nonce",
        )

        self.assertEqual("admin@example.org", authorization.identity.email)
        self.assertEqual("offline-refresh-token", authorization.refresh_token)
        response.json.return_value = {"id_token": "signed-token"}
        with self.assertRaises(PlatformAdminAuthError):
            client.exchange_gmail_code(
                code="authorization-code",
                redirect_uri="https://r2c-tracker.com/platform-admin/google/callback",
                verifier="pkce-verifier",
                expected_nonce="expected-nonce",
            )

    @patch("platform_admin_auth.requests.post")
    def test_gmail_sender_refreshes_access_and_sends_mime_message(self, post):
        token_response = Mock()
        token_response.json.return_value = {"access_token": "short-lived-access"}
        token_response.raise_for_status.return_value = None
        send_response = Mock()
        send_response.raise_for_status.return_value = None
        post.side_effect = [token_response, send_response]
        sender = GmailApiPlatformAdminEmailSender(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
            from_address="kjtsar@kjt.us",
        )

        sender.send_organization_password_reset(
            recipient="admin@example.org",
            organization_name="North County SAR",
            designator="NCSSAR",
            reset_url="https://r2c-tracker.com/ncssar/reset-password#token=signed",
        )

        self.assertEqual("refresh_token", post.call_args_list[0].kwargs["data"]["grant_type"])
        self.assertEqual(
            "Bearer short-lived-access",
            post.call_args_list[1].kwargs["headers"]["Authorization"],
        )
        raw = post.call_args_list[1].kwargs["json"]["raw"]
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        self.assertIn("Reset your NCSSAR", decoded)
        self.assertIn("15 minutes", decoded)

    def test_smtp_configuration_requires_tls_endpoint_sender_and_password_for_user(self):
        self.assertFalse(SmtpPlatformAdminEmailSender().is_configured)
        self.assertTrue(
            SmtpPlatformAdminEmailSender(
                host="smtp-relay.example.org",
                port=587,
                from_address="tracker@example.org",
            ).is_configured
        )

    @patch("platform_admin_auth.smtplib.SMTP")
    def test_organization_activation_email_explains_activation_and_uses_tls(self, smtp):
        sender = SmtpPlatformAdminEmailSender(
            host="smtp.example.org",
            port=587,
            username="tracker@example.org",
            password="secret manager value",
            from_address="tracker@example.org",
        )

        sender.send_organization_activation(
            recipient="admin@ncssar.example",
            administrator_name="Site Administrator",
            organization_name="North County SAR",
            designator="NCSSAR",
            activation_url="https://r2c-tracker.com/ncssar/activate?token=signed",
        )

        connection = smtp.return_value.__enter__.return_value
        connection.starttls.assert_called_once()
        connection.login.assert_called_once_with(
            "tracker@example.org",
            "secret manager value",
        )
        message = connection.send_message.call_args.args[0]
        self.assertIn("single-use link", message.get_content())
        self.assertIn("open-ended extended beta", message.get_content())
        self.assertIn("does not accept payments", message.get_content())
        self.assertIn("$10.00 calendar-month usage allowance", message.get_content())
        self.assertIn("Do not forward", message.get_content())
        self.assertNotIn("pilot", message.get_content().lower())
        self.assertEqual("admin@ncssar.example", message["To"])

    @patch("platform_admin_auth.smtplib.SMTP")
    def test_organization_password_reset_email_is_time_bounded(self, smtp):
        sender = SmtpPlatformAdminEmailSender(
            host="smtp.example.org",
            port=587,
            username="tracker@example.org",
            password="secret manager value",
            from_address="tracker@example.org",
        )
        sender.send_organization_password_reset(
            recipient="admin@ncssar.example",
            organization_name="North County SAR",
            designator="NCSSAR",
            reset_url="https://r2c-tracker.com/ncssar/reset-password#token=signed",
        )
        connection = smtp.return_value.__enter__.return_value
        connection.starttls.assert_called_once()
        message = connection.send_message.call_args.args[0]
        self.assertIn("15 minutes", message.get_content())
        self.assertIn("existing password has not been changed", message.get_content())

    @patch("platform_admin_auth.smtplib.SMTP")
    def test_member_activation_email_describes_role_scoped_access(self, smtp):
        sender = SmtpPlatformAdminEmailSender(
            host="smtp.example.org",
            port=587,
            username="tracker@example.org",
            password="secret manager value",
            from_address="tracker@example.org",
        )

        sender.send_organization_member_activation(
            recipient="viewer@ncssar.example",
            member_name="Records Viewer",
            organization_name="North County SAR",
            designator="NCSSAR",
            activation_url="https://r2c-tracker.com/ncssar/activate?token=signed",
        )

        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        content = message.get_content()
        self.assertEqual(
            "Activate your NCSSAR R2C Tracker membership",
            message["Subject"],
        )
        self.assertIn("Hello Records Viewer", content)
        self.assertIn("single-use link within seven days", content)
        self.assertIn("page permitted by your assigned roles", content)
        self.assertNotIn("trial", content.lower())
        self.assertNotIn("administrator", content.lower())

    @patch("platform_admin_auth.smtplib.SMTP")
    def test_active_organization_access_email_points_to_google_login(self, smtp):
        sender = SmtpPlatformAdminEmailSender(
            host="smtp.example.org",
            port=587,
            username="tracker@example.org",
            password="secret manager value",
            from_address="tracker@example.org",
        )

        sender.send_organization_access(
            recipient="admin@ncssar.example",
            administrator_name="Site Administrator",
            organization_name="North County SAR",
            designator="NCSSAR",
            login_url="https://r2c-tracker.com/ncssar/login",
        )

        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        content = message.get_content()
        self.assertIn("You have been assigned as the primary administrator", content)
        self.assertIn("https://r2c-tracker.com/ncssar/login", content)
        self.assertIn("Continue with Google", content)
        self.assertIn("No separate R2C Tracker username or password", content)
        self.assertIn(
            "contact the R2C Tracker platform administrator at tracker@example.org",
            content,
        )

    @patch("platform_admin_auth.smtplib.SMTP")
    def test_funding_exhausted_email_links_to_organization_admin(self, smtp):
        sender = SmtpPlatformAdminEmailSender(
            host="smtp.example.org",
            port=587,
            username="tracker@example.org",
            password="secret manager value",
            from_address="tracker@example.org",
        )

        sender.send_organization_funding_exhausted(
            recipient="admin@ncssar.example",
            administrator_name="Site Administrator",
            organization_name="North County SAR",
            designator="NCSSAR",
            grace_ends="03 Sep 2026",
            administration_url=(
                "https://r2c-tracker.com/ncssar/admin#service-status"
            ),
        )

        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        content = message.get_content()
        self.assertIn("funding has been consumed", message["Subject"])
        self.assertIn("30-day grace period through 03 Sep 2026", content)
        self.assertIn(
            "https://r2c-tracker.com/ncssar/admin#service-status",
            content,
        )
        self.assertIn("gross amount, Stripe processing fee, and net", content)

    @patch("platform_admin_auth.smtplib.SMTP")
    def test_lifecycle_deadline_email_explains_export_and_human_contact(self, smtp):
        sender = SmtpPlatformAdminEmailSender(
            host="smtp.example.org",
            port=587,
            username="tracker@example.org",
            password="secret manager value",
            from_address="tracker@example.org",
        )

        sender.send_organization_lifecycle_deadline(
            recipient="admin@ncssar.example",
            administrator_name="Site Administrator",
            organization_name="North County SAR",
            designator="NCSSAR",
            lifecycle_label="grace period",
            timing="ends within seven days",
            deadline="03 Sep 2026",
            administration_url="https://r2c-tracker.com/ncssar/admin#service-status",
            records_url="https://r2c-tracker.com/ncssar/admin/flights",
        )

        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        content = message.get_content()
        self.assertIn("grace period ends within seven days", message["Subject"])
        self.assertIn("does not automatically archive or shut down", content)
        self.assertIn("will contact your organization administrator", content)
        self.assertIn("Export as CSV", content)
        self.assertIn("Download Flight Log Archive", content)
        self.assertIn("https://r2c-tracker.com/ncssar/admin/flights", content)

    @patch("google.oauth2.id_token.verify_oauth2_token")
    @patch("platform_admin_auth.requests.post")
    def test_google_exchange_requires_verified_email_and_matching_nonce(
        self,
        post,
        verify_token,
    ):
        response = Mock()
        response.json.return_value = {"id_token": "signed-token"}
        response.raise_for_status.return_value = None
        post.return_value = response
        verify_token.return_value = {
            "sub": "google-subject",
            "email": "Admin@Example.org",
            "email_verified": True,
            "nonce": "expected-nonce",
            "name": "Administrator",
        }
        client = GoogleOidcClient("client-id", "client-secret")

        identity = client.exchange_code(
            code="authorization-code",
            redirect_uri="https://r2c-tracker.com/platform-admin/google/callback",
            verifier="pkce-verifier",
            expected_nonce="expected-nonce",
        )

        self.assertEqual("google-subject", identity.subject)
        self.assertEqual("admin@example.org", identity.email)
        verify_token.assert_called_once()
        post.assert_called_once()
        self.assertEqual(
            "pkce-verifier",
            post.call_args.kwargs["data"]["code_verifier"],
        )

        verify_token.return_value["nonce"] = "replayed-nonce"
        with self.assertRaises(PlatformAdminAuthError):
            client.exchange_code(
                code="authorization-code",
                redirect_uri="https://r2c-tracker.com/platform-admin/google/callback",
                verifier="pkce-verifier",
                expected_nonce="expected-nonce",
            )

        verify_token.return_value["nonce"] = "expected-nonce"
        verify_token.return_value["email_verified"] = False
        with self.assertRaises(PlatformAdminAuthError):
            client.exchange_code(
                code="authorization-code",
                redirect_uri="https://r2c-tracker.com/platform-admin/google/callback",
                verifier="pkce-verifier",
                expected_nonce="expected-nonce",
            )
        self.assertFalse(
            SmtpPlatformAdminEmailSender(
                host="smtp.example.org",
                port=587,
                username="tracker@example.org",
                from_address="tracker@example.org",
            ).is_configured
        )
        self.assertTrue(
            SmtpPlatformAdminEmailSender(
                host="smtp.example.org",
                port=587,
                username="tracker@example.org",
                password="secret manager value",
                from_address="tracker@example.org",
            ).is_configured
        )
