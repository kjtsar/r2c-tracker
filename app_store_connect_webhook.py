import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime


APPLE_SIGNATURE_PREFIX = "hmacsha256="
FEEDBACK_EVENT_TYPES = {
    "betaFeedbackCrashSubmissionCreated": "crash",
    "betaFeedbackScreenshotSubmissionCreated": "screenshot",
}
PING_EVENT_TYPE = "webhookPings"


class AppStoreConnectWebhookError(ValueError):
    pass


class AppStoreConnectSignatureError(AppStoreConnectWebhookError):
    pass


@dataclass(frozen=True)
class AppStoreConnectWebhookEvent:
    event_id: str
    event_type: str
    feedback_kind: str
    feedback_id: str
    resource_type: str
    timestamp: datetime
    resource_url: str


def verify_signature(body: bytes, signature: str, secret: str) -> None:
    clean_signature = signature.strip()
    if not secret or not clean_signature.startswith(APPLE_SIGNATURE_PREFIX):
        raise AppStoreConnectSignatureError("Invalid App Store Connect signature.")
    supplied_digest = clean_signature.removeprefix(APPLE_SIGNATURE_PREFIX)
    expected_digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_digest, expected_digest):
        raise AppStoreConnectSignatureError("Invalid App Store Connect signature.")


def parse_event(body: bytes) -> AppStoreConnectWebhookEvent | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppStoreConnectWebhookError(
            "App Store Connect sent malformed JSON."
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise AppStoreConnectWebhookError(
            "App Store Connect webhook data is missing."
        )
    data = payload["data"]
    event_type = str(data.get("type", "")).strip()
    if event_type == PING_EVENT_TYPE:
        return None
    feedback_kind = FEEDBACK_EVENT_TYPES.get(event_type)
    if not feedback_kind:
        raise AppStoreConnectWebhookError(
            "Unsupported App Store Connect webhook event type."
        )
    event_id = str(data.get("id", "")).strip()
    attributes = data.get("attributes")
    relationships = data.get("relationships")
    if not event_id or not isinstance(attributes, dict) or not isinstance(
        relationships, dict
    ):
        raise AppStoreConnectWebhookError(
            "App Store Connect webhook event is incomplete."
        )
    timestamp_text = str(attributes.get("timestamp", "")).strip()
    try:
        timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppStoreConnectWebhookError(
            "App Store Connect webhook timestamp is invalid."
        ) from exc
    if timestamp.tzinfo is None:
        raise AppStoreConnectWebhookError(
            "App Store Connect webhook timestamp must include a time zone."
        )
    instance = relationships.get("instance")
    if not isinstance(instance, dict) or not isinstance(instance.get("data"), dict):
        raise AppStoreConnectWebhookError(
            "App Store Connect webhook feedback reference is missing."
        )
    instance_data = instance["data"]
    feedback_id = str(instance_data.get("id", "")).strip()
    resource_type = str(instance_data.get("type", "")).strip()
    links = instance.get("links") if isinstance(instance.get("links"), dict) else {}
    resource_url = str(links.get("self", "")).strip()
    if not feedback_id or not resource_type:
        raise AppStoreConnectWebhookError(
            "App Store Connect webhook feedback reference is incomplete."
        )
    return AppStoreConnectWebhookEvent(
        event_id=event_id,
        event_type=event_type,
        feedback_kind=feedback_kind,
        feedback_id=feedback_id,
        resource_type=resource_type,
        timestamp=timestamp.astimezone(UTC),
        resource_url=resource_url,
    )


def authenticate_and_parse(
    body: bytes,
    signature: str,
    secret: str,
) -> AppStoreConnectWebhookEvent | None:
    verify_signature(body, signature, secret)
    return parse_event(body)
