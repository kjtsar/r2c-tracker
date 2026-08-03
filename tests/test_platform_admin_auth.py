import base64
import hashlib
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

from platform_admin_auth import (
    GoogleOidcClient,
    PlatformAdminAuthError,
    SmtpPlatformAdminEmailSender,
)


class PlatformAdminAuthHelpersTest(unittest.TestCase):
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
    def test_organization_activation_email_identifies_pilot_and_uses_tls(self, smtp):
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
        self.assertIn("pilot", message.get_content())
        self.assertIn("no payment is due", message.get_content())
        self.assertEqual("admin@ncssar.example", message["To"])

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
