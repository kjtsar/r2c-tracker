import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from app_store_connect_webhook import (
    AppStoreConnectSignatureError,
    AppStoreConnectWebhookError,
    authenticate_and_parse,
)


SECRET = "correct horse battery staple"  # pragma: allowlist secret


def feedback_payload(
    *,
    event_type="betaFeedbackCrashSubmissionCreated",
    event_id="a4319bc8-ed16-460b-8de6-ba9734b55631",
    feedback_id="AK7UjG-qL5QxXf3gIOGjbpQ",
):
    resource_type = (
        "betaFeedbackCrashSubmissions"
        if "Crash" in event_type
        else "betaFeedbackScreenshotSubmissions"
    )
    return {
        "data": {
            "type": event_type,
            "id": event_id,
            "version": 1,
            "attributes": {"timestamp": "2026-08-17T20:53:20.729Z"},
            "relationships": {
                "instance": {
                    "data": {"type": resource_type, "id": feedback_id},
                    "links": {
                        "self": (
                            "https://api.appstoreconnect.apple.com/v1/"
                            f"{resource_type}/{feedback_id}"
                        )
                    },
                }
            },
        }
    }


def encoded_payload(payload):
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def signature(body, secret=SECRET):
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"hmacsha256={digest}"


class AppStoreConnectWebhookParsingTest(unittest.TestCase):
    def test_authenticates_and_parses_crash_feedback(self):
        body = encoded_payload(feedback_payload())

        event = authenticate_and_parse(body, signature(body), SECRET)

        self.assertEqual("crash", event.feedback_kind)
        self.assertEqual("AK7UjG-qL5QxXf3gIOGjbpQ", event.feedback_id)
        self.assertEqual("2026-08-17T20:53:20.729000+00:00", event.timestamp.isoformat())

    def test_rejects_invalid_signature(self):
        body = encoded_payload(feedback_payload())

        with self.assertRaises(AppStoreConnectSignatureError):
            authenticate_and_parse(body, "hmacsha256=bad", SECRET)

    def test_accepts_authenticated_ping_without_creating_feedback(self):
        body = encoded_payload({
            "data": {
                "type": "webhookPings",
                "relationships": {
                    "webhook": {"data": {"type": "webhooks", "id": "webhook-id"}}
                },
            }
        })

        self.assertIsNone(authenticate_and_parse(body, signature(body), SECRET))

    def test_rejects_unsubscribed_event_type(self):
        body = encoded_payload({"data": {"type": "buildUploadStateUpdated"}})

        with self.assertRaises(AppStoreConnectWebhookError):
            authenticate_and_parse(body, signature(body), SECRET)


class FakeWebhookStore:
    def __init__(self, claim="claimed"):
        self.claim = claim
        self.claimed = []
        self.sent = []
        self.failed = []

    async def claim_external_webhook_delivery(self, **values):
        self.claimed.append(values)
        return self.claim

    async def mark_external_webhook_delivery_sent(self, **values):
        self.sent.append(values)

    async def mark_external_webhook_delivery_failed(self, **values):
        self.failed.append(values)


class FakeFeedbackEmailSender:
    is_configured = True

    def __init__(self, error=None):
        self.error = error
        self.messages = []

    def send_testflight_feedback(self, **message):
        if self.error:
            raise self.error
        self.messages.append(message)


class AppStoreConnectWebhookRouteTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def post(self, payload, *, secret=SECRET):
        body = encoded_payload(payload)
        return self.client.post(
            "/webhooks/app-store-connect",
            content=body,
            headers={
                "content-type": "application/json",
                "x-apple-signature": signature(body, secret),
            },
        )

    def configured(self, store, sender):
        return (
            patch.object(main, "APP_STORE_CONNECT_WEBHOOK_SECRET", SECRET),
            patch.object(main, "TESTFLIGHT_FEEDBACK_EMAIL", "kjtsar@kjt.us"),
            patch.object(main, "control_plane_store", store),
            patch.object(main, "platform_admin_email_sender", sender),
        )

    def test_valid_feedback_is_emailed_and_marked_sent(self):
        store = FakeWebhookStore()
        sender = FakeFeedbackEmailSender()
        patches = self.configured(store, sender)

        with patches[0], patches[1], patches[2], patches[3]:
            response = self.post(feedback_payload())

        self.assertEqual(204, response.status_code)
        self.assertEqual(1, len(sender.messages))
        self.assertEqual("kjtsar@kjt.us", sender.messages[0]["recipient"])
        self.assertEqual("crash", sender.messages[0]["feedback_kind"])
        self.assertEqual(1, len(store.sent))
        self.assertEqual([], store.failed)

    def test_duplicate_event_is_acknowledged_without_second_email(self):
        store = FakeWebhookStore(claim="sent")
        sender = FakeFeedbackEmailSender()
        patches = self.configured(store, sender)

        with patches[0], patches[1], patches[2], patches[3]:
            response = self.post(feedback_payload())

        self.assertEqual(204, response.status_code)
        self.assertEqual([], sender.messages)

    def test_delivery_failure_is_recorded_and_asks_apple_to_retry(self):
        store = FakeWebhookStore()
        sender = FakeFeedbackEmailSender(RuntimeError("mail unavailable"))
        patches = self.configured(store, sender)

        with patches[0], patches[1], patches[2], patches[3]:
            response = self.post(feedback_payload())

        self.assertEqual(503, response.status_code)
        self.assertEqual(1, len(store.failed))
        self.assertIn("mail unavailable", store.failed[0]["error"])

    def test_ping_checks_authentication_but_does_not_send_email(self):
        store = FakeWebhookStore()
        sender = FakeFeedbackEmailSender()
        payload = {
            "data": {
                "type": "webhookPings",
                "relationships": {
                    "webhook": {"data": {"type": "webhooks", "id": "webhook-id"}}
                },
            }
        }
        patches = self.configured(store, sender)

        with patches[0], patches[1], patches[2], patches[3]:
            response = self.post(payload)

        self.assertEqual(204, response.status_code)
        self.assertEqual([], store.claimed)
        self.assertEqual([], sender.messages)

    def test_invalid_signature_is_rejected_before_claim(self):
        store = FakeWebhookStore()
        sender = FakeFeedbackEmailSender()
        patches = self.configured(store, sender)

        with patches[0], patches[1], patches[2], patches[3]:
            response = self.post(feedback_payload(), secret="wrong secret")

        self.assertEqual(401, response.status_code)
        self.assertEqual([], store.claimed)
        self.assertEqual([], sender.messages)


if __name__ == "__main__":
    unittest.main()
