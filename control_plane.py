import base64
import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from calendar import monthrange
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import (
    Boolean,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    Numeric,
    inspect,
    String,
    Text,
    UniqueConstraint,
    delete,
    event,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


DESIGNATOR_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLE_DESCRIPTIONS = {
    "organization_owner": (
        "Full organization authority, including policies, members, enrollment, "
        "configuration releases, flight records, and billing information."
    ),
    "billing_admin": (
        "View organization service status, usage, allocated costs, and account "
        "activity."
    ),
    "config_admin": (
        "Pull configuration from a connected RID2Caltopo device; review, approve, "
        "discard, and restore CalTopo credential and drone-list releases."
    ),
    "user_admin": (
        "Add, edit, invite, disable, and restore organization members, and manage "
        "device-enrollment campaigns."
    ),
    "records_admin": (
        "View, export, import, delete, and restore this organization's flight "
        "records and logs."
    ),
    "records_viewer": (
        "View this organization's flight dashboard when its records are restricted."
    ),
    "r2c_device": (
        "Enroll and operate RID2Caltopo devices for this organization. This role "
        "automatically includes flight-record viewing."
    ),
    "video_requester": (
        "View advertised streams and request organization video; the pilot must "
        "still approve each request."
    ),
}
ROLE_NAMES = frozenset(ROLE_DESCRIPTIONS)
DEFAULT_OWNER_ROLES = (
    "billing_admin",
    "config_admin",
    "organization_owner",
    "records_admin",
    "records_viewer",
    "r2c_device",
    "user_admin",
    "video_requester",
)
ONBOARDING_STEPS = (
    "reserve organization identity and tenant path",
    "prepare tenant database boundary",
    "prepare tenant object-storage boundary",
    "prepare tenant secret references",
    "prepare tenant path routing",
    "run tenant health checks",
    "prepare administrator activation",
)
MANAGED_ACCESS_TERMS_VERSION = "2026-08-07"

# A preflight can under-report when the synthetic TURN probe itself stalls.
# Keep the exception deliberately narrow: only the smallest field-usable
# profile may exceed the measured result.
EMERGENCY_VIDEO_MAX_LONG_EDGE = 640
EMERGENCY_VIDEO_MAX_FPS = 5.0
EMERGENCY_VIDEO_MAX_BITRATE_BPS = 200_000
TABLET_LINK_CODE_DIGEST_BYTES = 4
VIDEO_RESPONSE_TIMEOUT_SECONDS = 60
VIDEO_SESSION_AUTHORIZATION_SECONDS = 10 * 60
RECORDING_APPROVAL_TIMEOUT_SECONDS = 60
RECORDING_TRANSFER_TIMEOUT_SECONDS = 15 * 60
AUDIT_EVENT_RETENTION_DAYS = 365
AUDIT_EVENT_HOT_DAYS = 90
AUDIT_EVENT_RECENT_DAYS = 30
AUDIT_EVENT_RECENT_LIMIT = 25
AUDIT_EVENT_PAGE_SIZE = 50
AUDIT_EVENT_EXPORT_LIMIT = 10_000
EXTENDED_BETA_MONTHLY_ALLOWANCE = Decimal("10.00")
EXTENDED_BETA_VIDEO_CUTOFF = Decimal("9.00")
AUDIT_EVENT_CATEGORY_PREFIXES = {
    "administration": ("organization.", "administrator.", "member."),
    "billing": ("billing.",),
    "enrollment": ("enrollment.", "device."),
    "video": ("video.",),
    "recording": ("recording.",),
    "audit": ("audit.",),
}


def tablet_link_code(organization_designator: str, device_name: str) -> str:
    """Return the 32-bit base64url alias for a tablet's canonical path."""
    material = (
        f"/{organization_designator.strip().lower()}"
        f"/streams/{device_name.strip().lower()}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(
        digest[:TABLET_LINK_CODE_DIGEST_BYTES]
    ).decode("ascii").rstrip("=")


def stream_link_code(
    organization_designator: str,
    device_name: str,
    video_stream: str,
) -> str:
    """Return the 32-bit base64url alias for one canonical stream path."""
    material = (
        f"/{organization_designator.strip().lower()}"
        f"/streams/{device_name.strip().lower()}"
        f"/{video_stream.strip().lower()}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(
        digest[:TABLET_LINK_CODE_DIGEST_BYTES]
    ).decode("ascii").rstrip("=")


def recording_link_code(
    organization_designator: str,
    device_name: str,
    session_id: str,
) -> str:
    """Return the short alias for one stable recording session."""
    material = (
        f"/{organization_designator.strip().lower()}"
        f"/streams/{device_name.strip().lower()}"
        f"/session/{session_id.strip().lower()}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(
        digest[:TABLET_LINK_CODE_DIGEST_BYTES]
    ).decode("ascii").rstrip("=")


def is_emergency_video_fallback(
    *,
    width: int,
    height: int,
    fps_milli: int,
    bitrate_bps: int,
) -> bool:
    return (
        width > 0
        and height > 0
        and max(width, height) <= EMERGENCY_VIDEO_MAX_LONG_EDGE
        and 0 < fps_milli <= int(EMERGENCY_VIDEO_MAX_FPS * 1000)
        and 0 < bitrate_bps <= EMERGENCY_VIDEO_MAX_BITRATE_BPS
    )


class ControlPlaneError(Exception):
    pass


class DuplicateOrganizationError(ControlPlaneError):
    pass


class InvalidOrganizationError(ControlPlaneError):
    pass


def require_separate_database(control_plane_url: str, tenant_database_url: str) -> None:
    if (
        control_plane_url
        and tenant_database_url
        and control_plane_url.strip() == tenant_database_url.strip()
    ):
        raise ValueError(
            "CONTROL_PLANE_DATABASE_URL must not be the tenant DATABASE_URL."
        )


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def device_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    designator: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    lifecycle_state: Mapped[str] = mapped_column(String(32), default="extended_beta")
    provisioning_state: Mapped[str] = mapped_column(
        String(32), default="simulation pending"
    )
    billing_mode: Mapped[str] = mapped_column(String(32), default="shadow billing")
    trial_starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    records_visibility: Mapped[str] = mapped_column(String(24), default="restricted")
    record_retention_days: Mapped[int] = mapped_column(Integer, default=730)
    log_retention_days: Mapped[int] = mapped_column(Integer, default=30)
    notification_email: Mapped[str] = mapped_column(String(320), default="")
    archived_from_lifecycle_state: Mapped[str] = mapped_column(String(32), default="")
    archived_from_provisioning_state: Mapped[str] = mapped_column(String(32), default="")
    archived_from_subscription_state: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class OrganizationContact(Base):
    __tablename__ = "organization_contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    contact_role: Mapped[str] = mapped_column(String(32), default="primary_admin")
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    phone: Mapped[str] = mapped_column(String(64), default="")
    postal_address: Mapped[str] = mapped_column(Text, default="")
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class ManagedAccessRequest(Base):
    __tablename__ = "managed_access_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    requester_name: Mapped[str] = mapped_column(String(160), nullable=False)
    requester_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    requester_phone: Mapped[str] = mapped_column(String(64), default="")
    organization_name: Mapped[str] = mapped_column(String(200), nullable=False)
    designator: Mapped[str] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    source_host: Mapped[str] = mapped_column(String(255), default="")
    terms_version: Mapped[str] = mapped_column(String(32), default="")
    terms_acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class OrganizationUser(Base):
    __tablename__ = "organization_users"
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_org_user_email"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, default="")
    roles_json: Mapped[str] = mapped_column(Text, default="[]")
    state: Mapped[str] = mapped_column(String(24), default="invited")
    activation_nonce: Mapped[str] = mapped_column(String(64), default=new_id)
    activation_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    @property
    def roles(self) -> tuple[str, ...]:
        values = json.loads(self.roles_json or "[]")
        normalized_roles = {value for value in values if value in ROLE_NAMES}
        if "organization_owner" in normalized_roles:
            normalized_roles.add("r2c_device")
        if "r2c_device" in normalized_roles:
            normalized_roles.add("records_viewer")
        return tuple(sorted(normalized_roles))

    def set_roles(self, roles: tuple[str, ...]) -> None:
        invalid = set(roles) - ROLE_NAMES
        if invalid:
            raise ValueError(f"Unknown organization roles: {sorted(invalid)}")
        normalized_roles = set(roles)
        if "r2c_device" in normalized_roles:
            normalized_roles.add("records_viewer")
        self.roles_json = json.dumps(sorted(normalized_roles))


class OrganizationExternalIdentity(Base):
    __tablename__ = "organization_external_identities"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "provider",
            "issuer",
            "subject",
            name="uq_org_external_identity_subject",
        ),
        UniqueConstraint(
            "user_id",
            "provider",
            name="uq_org_external_identity_user_provider",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("organization_users.id"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class OrganizationLoginThrottle(Base):
    __tablename__ = "organization_login_throttles"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "email",
            name="uq_org_login_throttle",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    locked_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


class OrganizationPasswordResetToken(Base):
    __tablename__ = "organization_password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("organization_users.id"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class OrganizationPasswordResetThrottle(Base):
    __tablename__ = "organization_password_reset_throttles"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "email",
            name="uq_org_password_reset_throttle",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    last_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


class PlatformAdminUser(Base):
    __tablename__ = "platform_admin_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


class PlatformAdminLoginThrottle(Base):
    __tablename__ = "platform_admin_login_throttles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    locked_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


class PlatformAdminPasswordSetupToken(Base):
    __tablename__ = "platform_admin_password_setup_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    identity_generation: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class PlatformAdminPasswordSetupThrottle(Base):
    __tablename__ = "platform_admin_password_setup_throttles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    last_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


class ProvisioningJob(Base):
    __tablename__ = "provisioning_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    state: Mapped[str] = mapped_column(String(24), default="queued")
    current_step: Mapped[str] = mapped_column(String(160), default="")
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    simulation: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(24), default="extended_beta")
    collection_method: Mapped[str] = mapped_column(
        String(32), default="not configured"
    )
    billing_cadence: Mapped[str] = mapped_column(
        String(24), default="not configured"
    )
    external_customer_id: Mapped[str] = mapped_column(String(160), default="")
    external_subscription_id: Mapped[str] = mapped_column(String(160), default="")
    trial_starts_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class UsageDaily(Base):
    __tablename__ = "usage_daily"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "usage_date",
            name="uq_usage_daily_organization_date",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False)
    compute_units: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal("0")
    )
    compute_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    network_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    network_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    storage_byte_days: Mapped[int] = mapped_column(BigInteger, default=0)
    storage_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    database_units: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal("0")
    )
    database_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    faa_proxy_requests: Mapped[int] = mapped_column(Integer, default=0)
    faa_proxy_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    turn_relay_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    turn_relay_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    other_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class ExtendedBetaAllowance(Base):
    __tablename__ = "extended_beta_allowances"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "billing_month", name="uq_beta_allowance_org_month"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    billing_month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    allowance_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=EXTENDED_BETA_MONTHLY_ALLOWANCE
    )
    actual_cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    forecast_cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    billing_data_through: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    video_disabled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    month_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class BillingLedgerEntry(Base):
    __tablename__ = "billing_ledger"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_billing_ledger_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    description: Mapped[str] = mapped_column(String(240), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    external_reference: Mapped[str] = mapped_column(String(200), default="")
    created_by_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class BillingNotification(Base):
    __tablename__ = "billing_notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    notification_type: Mapped[str] = mapped_column(String(48), nullable=False)
    event_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(240), default="")
    deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ControlPlaneAuditEvent(Base):
    __tablename__ = "control_plane_audit_events"
    __table_args__ = (
        Index("idx_control_plane_audit_events_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    retention_hold: Mapped[bool] = mapped_column(Boolean, default=False)


class ExternalWebhookDelivery(Base):
    __tablename__ = "external_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "event_id",
            name="uq_external_webhook_delivery_provider_event",
        ),
        Index("idx_external_webhook_deliveries_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="processing")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class EnrollmentCampaign(Base):
    __tablename__ = "enrollment_campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("organization_users.id")
    )
    state: Mapped[str] = mapped_column(String(24), default="active")
    max_redemptions: Mapped[int] = mapped_column(Integer, default=25)
    redemption_count: Mapped[int] = mapped_column(Integer, default=0)
    token_generation: Mapped[str] = mapped_column(
        String(36), default="", nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class DeviceCredential(Base):
    __tablename__ = "device_credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("enrollment_campaigns.id"), index=True
    )
    device_name: Mapped[str] = mapped_column(String(160), nullable=False)
    platform: Mapped[str] = mapped_column(String(24), nullable=False)
    authorized_user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("organization_users.id"), index=True, nullable=True
    )
    functionality_release: Mapped[int] = mapped_column(Integer, default=0)
    token_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    reauth_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


class OrganizationConfigState(Base):
    __tablename__ = "organization_config_states"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), primary_key=True
    )
    current_version_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class OrganizationConfigProposal(Base):
    __tablename__ = "organization_config_proposals"

    # There is deliberately at most one proposed configuration per organization.
    # Re-requesting from a device replaces the unapproved proposal.
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(36), unique=True, index=True, default=new_id)
    source_device_credential_id: Mapped[str] = mapped_column(
        ForeignKey("device_credentials.id"), index=True
    )
    source_device_name: Mapped[str] = mapped_column(String(160), nullable=False)
    requested_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("organization_users.id")
    )
    state: Mapped[str] = mapped_column(String(24), default="awaiting_device")
    snapshot_json: Mapped[str] = mapped_column(Text, default="")
    diff_json: Mapped[str] = mapped_column(Text, default="{}")


class OrganizationConfigRelease(Base):
    __tablename__ = "organization_config_releases"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "version_ms", name="uq_org_config_release_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    version_ms: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_device_credential_id: Mapped[str] = mapped_column(
        ForeignKey("device_credentials.id")
    )
    source_device_name: Mapped[str] = mapped_column(String(160), nullable=False)
    approved_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("organization_users.id")
    )
    comment: Mapped[str] = mapped_column(String(1000), default="")


class ActiveVideoStream(Base):
    __tablename__ = "active_video_streams"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_active_video_stream_session"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    device_credential_id: Mapped[str] = mapped_column(
        ForeignKey("device_credentials.id"), index=True
    )
    device_name: Mapped[str] = mapped_column(
        String(160), nullable=False, default="Unknown device"
    )
    incident_name: Mapped[str] = mapped_column(String(160), nullable=False)
    drone_designator: Mapped[str] = mapped_column(String(160), nullable=False)
    source_width: Mapped[int] = mapped_column(Integer, default=0)
    source_height: Mapped[int] = mapped_column(Integer, default=0)
    source_fps_milli: Mapped[int] = mapped_column(Integer, default=0)
    source_bitrate_bps: Mapped[int] = mapped_column(BigInteger, default=0)
    source_codec: Mapped[str] = mapped_column(String(32), default="")
    media_kind: Mapped[str] = mapped_column(String(16), default="live")
    recorded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    thumbnail_revision: Mapped[str] = mapped_column(String(64), default="")
    timezone_name: Mapped[str] = mapped_column(String(64), default="UTC")
    remote_control_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )


class VideoStreamRequest(Base):
    __tablename__ = "video_stream_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    active_stream_id: Mapped[str] = mapped_column(
        ForeignKey("active_video_streams.id"), index=True
    )
    requester_user_id: Mapped[str] = mapped_column(
        ForeignKey("organization_users.id"), index=True
    )
    requester_email: Mapped[str] = mapped_column(String(320), nullable=False)
    remote_control_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(24), default="pending")
    status_message: Mapped[str] = mapped_column(String(400), default="")
    route_kind: Mapped[str] = mapped_column(String(16), default="unknown")
    estimated_uplink_bps: Mapped[int] = mapped_column(BigInteger, default=0)
    quality_source_width: Mapped[int] = mapped_column(Integer, default=0)
    quality_source_height: Mapped[int] = mapped_column(Integer, default=0)
    quality_source_fps_milli: Mapped[int] = mapped_column(Integer, default=0)
    selected_width: Mapped[int] = mapped_column(Integer, default=0)
    selected_height: Mapped[int] = mapped_column(Integer, default=0)
    selected_fps_milli: Mapped[int] = mapped_column(Integer, default=0)
    selected_bitrate_bps: Mapped[int] = mapped_column(BigInteger, default=0)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    audio_bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    audio_bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    video_bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)


class RecordingDownloadRequest(Base):
    __tablename__ = "recording_download_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    active_stream_id: Mapped[str] = mapped_column(ForeignKey("active_video_streams.id"), index=True)
    requester_user_id: Mapped[str] = mapped_column(ForeignKey("organization_users.id"), index=True)
    requester_email: Mapped[str] = mapped_column(String(320), nullable=False)
    device_credential_id: Mapped[str] = mapped_column(ForeignKey("device_credentials.id"), index=True)
    remote_control_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(24), default="awaiting_approval", index=True)
    status_message: Mapped[str] = mapped_column(String(400), default="")
    filename: Mapped[str] = mapped_column(String(240), default="")
    media_type: Mapped[str] = mapped_column(String(120), default="video/mp4")
    byte_count: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    storage_relpath: Mapped[str] = mapped_column(String(500), default="")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class VideoPreflightExchange(Base):
    __tablename__ = "video_preflight_exchanges"

    request_id: Mapped[str] = mapped_column(
        ForeignKey("video_stream_requests.id"),
        primary_key=True,
    )
    browser_offer_sdp: Mapped[str] = mapped_column(Text, nullable=False)
    device_answer_sdp: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )


class VideoMediaExchange(Base):
    """Short-lived WHEP signaling; media never traverses the tracker."""

    __tablename__ = "video_media_exchanges"

    request_id: Mapped[str] = mapped_column(
        ForeignKey("video_stream_requests.id"),
        primary_key=True,
    )
    browser_offer_sdp: Mapped[str] = mapped_column(Text, nullable=False)
    device_answer_sdp: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class VideoMediaMetricsSegment(Base):
    """Last observed WebRTC counters for one browser media connection."""

    __tablename__ = "video_media_metrics_segments"

    request_id: Mapped[str] = mapped_column(
        ForeignKey("video_stream_requests.id"),
        primary_key=True,
    )
    metrics_session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    audio_bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    audio_bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    video_bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@dataclass(frozen=True)
class OrganizationRecord:
    id: str
    legal_name: str
    designator: str
    hostname: str
    lifecycle_state: str
    provisioning_state: str
    billing_mode: str
    trial_ends_at: Optional[datetime]
    records_visibility: str
    record_retention_days: int
    log_retention_days: int
    notification_email: str
    primary_admin_name: str
    primary_admin_email: str
    subscription_state: str = "extended_beta"
    credit_balance: Decimal = Decimal("0.00")
    primary_admin_postal_address: str = ""
    primary_admin_phone: str = ""


@dataclass(frozen=True)
class ManagedAccessRequestRecord:
    id: str
    requester_name: str
    requester_email: str
    requester_phone: str
    organization_name: str
    designator: str
    state: str
    source_host: str
    terms_version: str
    terms_acknowledged_at: Optional[datetime]
    submitted_at: datetime


@dataclass(frozen=True)
class AdministratorUpdateRecord:
    organization: OrganizationRecord
    old_name: str
    old_email: str
    old_phone: str
    administrator_changed: bool


@dataclass(frozen=True)
class UserRecord:
    id: str
    organization_id: str
    email: str
    display_name: str
    roles: tuple[str, ...]
    state: str


@dataclass(frozen=True)
class PlatformAdminRecord:
    id: str
    email: str
    display_name: str
    state: str


@dataclass(frozen=True)
class InvitationRecord:
    user_id: str
    organization_id: str
    designator: str
    email: str
    activation_nonce: str
    expires_at: datetime


@dataclass(frozen=True)
class EnrollmentCampaignRecord:
    id: str
    organization_id: str
    label: str
    state: str
    max_redemptions: int
    redemption_count: int
    expires_at: datetime
    created_at: datetime
    revoked_at: Optional[datetime]
    token_generation: str = ""

    def is_usable(self, now: Optional[datetime] = None) -> bool:
        checked_at = now or utc_now()
        return (
            self.state == "active"
            and self.expires_at >= checked_at
            and self.redemption_count < self.max_redemptions
        )


@dataclass(frozen=True)
class BillingLedgerRecord:
    id: str
    entry_type: str
    amount: Decimal
    currency: str
    description: str
    external_reference: str
    created_at: datetime


@dataclass(frozen=True)
class BillingNotificationRecord:
    id: str
    organization_id: str
    designator: str
    organization_name: str
    administrator_name: str
    administrator_email: str
    notification_type: str
    deadline_at: Optional[datetime]


@dataclass(frozen=True)
class ExtendedBetaAllowanceRecord:
    organization_id: str
    billing_month: str
    allowance_amount: Decimal
    actual_cost: Decimal
    forecast_cost: Decimal
    billing_data_through: datetime
    video_disabled_at: Optional[datetime]
    month_ends_at: datetime

    @property
    def video_streaming_allowed(self) -> bool:
        return self.video_disabled_at is None


@dataclass(frozen=True)
class UsageCostRecord:
    organization_id: str
    compute: Decimal
    network: Decimal
    storage: Decimal
    database: Decimal
    other: Decimal

    @property
    def total(self) -> Decimal:
        return self.compute + self.network + self.storage + self.database + self.other


@dataclass(frozen=True)
class UsageAggregateRecord:
    organization_id: str
    compute_units: Decimal = Decimal("0")
    network_bytes: int = 0
    storage_byte_days: int = 0
    database_units: Decimal = Decimal("0")
    faa_proxy_requests: int = 0
    turn_relay_bytes: int = 0


@dataclass(frozen=True)
class ProvisioningJobRecord:
    id: str
    organization_id: str
    designator: str
    state: str
    current_step: str
    simulation: bool
    steps: tuple[dict, ...]
    created_at: datetime
    completed_at: Optional[datetime]


@dataclass(frozen=True)
class AuditEventRecord:
    id: str
    organization_id: Optional[str]
    designator: Optional[str]
    actor_type: str
    actor_id: str
    event_type: str
    details: dict
    created_at: datetime
    retention_hold: bool


@dataclass(frozen=True)
class AuditEventPage:
    events: tuple[AuditEventRecord, ...]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


@dataclass(frozen=True)
class IssuedDeviceCredential:
    id: str
    organization_id: str
    designator: str
    token: str
    device_name: str
    platform: str
    expires_at: datetime


@dataclass(frozen=True)
class DeviceCredentialRecord:
    id: str
    organization_id: str
    designator: str
    device_name: str
    platform: str
    expires_at: datetime
    functionality_release: int = 0


@dataclass(frozen=True)
class DeviceCredentialAdminRecord:
    id: str
    organization_id: str
    device_name: str
    platform: str
    authorized_user_id: Optional[str]
    functionality_release: int
    state: str
    created_at: datetime
    expires_at: datetime
    last_used_at: Optional[datetime]
    reauth_requested_at: Optional[datetime]


@dataclass(frozen=True)
class OrganizationConfigProposalRecord:
    id: str
    organization_id: str
    source_device_credential_id: str
    source_device_name: str
    requested_by_user_id: str
    state: str
    snapshot: dict
    diff: dict


@dataclass(frozen=True)
class OrganizationConfigReleaseRecord:
    organization_id: str
    version_ms: int
    snapshot: dict
    source_device_credential_id: str
    source_device_name: str
    approved_by_user_id: str
    comment: str


@dataclass(frozen=True)
class ActiveVideoStreamRecord:
    id: str
    session_id: str
    organization_id: str
    device_credential_id: str
    device_name: str
    incident_name: str
    drone_designator: str
    source_width: int
    source_height: int
    source_fps: float
    source_bitrate_bps: int
    source_codec: str
    media_kind: str
    recorded_at: Optional[datetime]
    duration_ms: int
    thumbnail_revision: str
    timezone_name: str
    remote_control_enabled: bool
    last_seen_at: datetime
    expires_at: datetime

    @property
    def recorded_at_local(self) -> Optional[datetime]:
        if self.recorded_at is None:
            return None
        try:
            zone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("UTC")
        return as_utc(self.recorded_at).astimezone(zone)


@dataclass(frozen=True)
class VideoStreamRequestRecord:
    id: str
    organization_id: str
    device_credential_id: str
    device_name: str
    stream_session_id: str
    incident_name: str
    drone_designator: str
    requester_user_id: str
    requester_email: str
    source_width: int
    source_height: int
    source_fps: float
    source_bitrate_bps: int
    source_codec: str
    timezone_name: str
    remote_control_enabled: bool
    state: str
    status_message: str
    route_kind: str
    estimated_uplink_bps: int
    selected_width: int
    selected_height: int
    selected_fps: float
    selected_bitrate_bps: int
    requested_at: datetime
    expires_at: datetime
    started_at: Optional[datetime]
    stopped_at: Optional[datetime]
    audio_bytes_sent: int
    audio_bytes_received: int
    video_bytes_received: int

    @property
    def requested_at_local(self) -> datetime:
        try:
            zone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("UTC")
        return as_utc(self.requested_at).astimezone(zone)

    @property
    def duration_seconds(self) -> Optional[int]:
        if self.started_at is None:
            return None
        ended_at = self.stopped_at or utc_now()
        return max(0, int((as_utc(ended_at) - as_utc(self.started_at)).total_seconds()))

    @property
    def total_media_bytes(self) -> int:
        return max(0, self.audio_bytes_sent) + max(0, self.audio_bytes_received) + max(0, self.video_bytes_received)


@dataclass(frozen=True)
class RecordingDownloadRequestRecord:
    id: str
    organization_id: str
    device_credential_id: str
    device_name: str
    stream_session_id: str
    drone_designator: str
    requester_user_id: str
    requester_email: str
    remote_control_enabled: bool
    state: str
    status_message: str
    filename: str
    media_type: str
    byte_count: int
    sha256: str
    storage_relpath: str
    requested_at: datetime
    expires_at: datetime
    completed_at: Optional[datetime]


@dataclass(frozen=True)
class VideoPreflightExchangeRecord:
    request_id: str
    organization_id: str
    device_credential_id: str
    requester_user_id: str
    state: str
    status_message: str
    route_kind: str
    estimated_uplink_bps: int
    remote_control_enabled: bool
    source_width: int
    source_height: int
    source_fps: float
    browser_offer_sdp: str
    device_answer_sdp: str
    expires_at: datetime


@dataclass(frozen=True)
class VideoMediaExchangeRecord:
    request_id: str
    organization_id: str
    device_credential_id: str
    requester_user_id: str
    stream_session_id: str
    requester_email: str
    route_kind: str
    selected_width: int
    selected_height: int
    selected_fps: float
    selected_bitrate_bps: int
    state: str
    status_message: str
    browser_offer_sdp: str
    device_answer_sdp: str
    expires_at: datetime


def managed_video_quality_choices(
    *,
    source_width: int,
    source_height: int,
    source_fps: float,
    usable_uplink_bps: int,
) -> tuple[dict, ...]:
    """Return the cross-platform managed-video quality policy."""
    width = source_width if source_width > 0 else 1280
    height = source_height if source_height > 0 else 720
    fps = source_fps if source_fps > 0 else 30.0
    long_edge = max(width, height)
    presets = (
        ("High", 1280, 30.0, 2_500_000),
        ("Balanced", 960, 15.0, 1_200_000),
        ("Low", 640, 10.0, 500_000),
        ("Emergency", 640, 5.0, 200_000),
    )
    choices = []
    usable = max(0, int(usable_uplink_bps))
    for name, preset_edge, preset_fps, preset_bitrate in presets:
        target_edge = min(long_edge, preset_edge)
        scale = target_edge / long_edge
        selected_width = max(2, int(width * scale)) & ~1
        selected_height = max(2, int(height * scale)) & ~1
        selected_fps = min(fps, preset_fps)
        reference_pixels = (
            preset_edge * preset_edge * min(width, height) / long_edge
        )
        pixel_scale = min(
            1.0,
            selected_width * selected_height / max(1.0, reference_pixels),
        )
        rate_scale = min(1.0, selected_fps / preset_fps)
        minimum_bitrate = 100_000 if name == "Emergency" else 150_000
        bitrate = max(
            minimum_bitrate,
            min(preset_bitrate, int(preset_bitrate * pixel_scale * rate_scale)),
        )
        capacity = (
            "enough"
            if usable * 100 >= bitrate * 135
            else "marginal"
            if usable >= bitrate
            else "insufficient"
        )
        choices.append({
            "preset": name,
            "width": selected_width,
            "height": selected_height,
            "fps": selected_fps,
            "bitrateBps": bitrate,
            "capacity": capacity,
        })
    if all(choice["capacity"] == "insufficient" for choice in choices):
        fallback = min(
            choices,
            key=lambda choice: (
                choice["bitrateBps"],
                max(choice["width"], choice["height"]),
                choice["fps"],
            ),
        )
        fallback["capacity"] = "fallback"
    return tuple(choices)


def normalize_designator(value: str) -> str:
    designator = value.strip().upper()
    if not DESIGNATOR_RE.fullmatch(designator):
        raise InvalidOrganizationError(
            "Designator must be 2-16 uppercase letters or digits and begin "
            "with a letter."
        )
    return designator


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise InvalidOrganizationError("Enter a valid administrator email address.")
    return email


def normalize_session_description(value: str) -> str:
    description = value.strip()
    # Some browser/WebView combinations submit the SDP with escaped newline
    # sequences.  Passing that one-line value to native WebRTC produces an
    # opaque "SessionDescription is NULL" error.  Normalize both escaped and
    # real line endings to the CRLF form required by SDP.
    if "\n" not in description and "\\n" in description:
        description = description.replace("\\r\\n", "\n").replace("\\n", "\n")
    description = "\r\n".join(description.splitlines()) + "\r\n"
    if (
        not description.startswith("v=0")
        or len(description) > 262_144
        or "\x00" in description
        or "\r\n" not in description
    ):
        raise ControlPlaneError("WebRTC session description is invalid.")
    return description


def normalize_video_preflight_answer(value: str) -> str:
    """Return a conservative browser-compatible SDP answer.

    Apple's WebRTC stack advertises the optional SCTP max-message-size
    attribute.  Some Chromium builds reject an otherwise valid answer when
    that attribute is the final SDP line.  The synthetic preflight uses small
    data-channel messages, so omitting the optional advertisement preserves
    the probe while avoiding that browser-specific parser dependency.
    """
    description = normalize_session_description(value)
    lines = [
        line
        for line in description.splitlines()
        if not line.startswith("a=max-message-size:")
    ]
    return "\r\n".join(lines) + "\r\n"


def normalize_timezone_name(value: str) -> str:
    timezone_name = value.strip() or "UTC"
    if len(timezone_name) > 64:
        raise ControlPlaneError("Tablet timezone name is too long.")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ControlPlaneError("Tablet timezone is not recognized.") from exc
    return timezone_name


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(digest_hex)),
        )
        return secrets.compare_digest(candidate, bytes.fromhex(digest_hex))
    except (TypeError, ValueError):
        return False


DUMMY_PASSWORD_HASH = hash_password(
    "r2c constant-time unknown organization user password"
)


class ControlPlaneStore:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("A separate control-plane database URL is required.")
        # Cloud SQL can close an otherwise idle TCP connection while a
        # scale-to-zero Cloud Run instance still retains its SQLAlchemy pool.
        # Validate connections when they are checked out and recycle them
        # before common infrastructure idle limits are reached.
        self.engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        if database_url.startswith("sqlite"):
            event.listen(
                self.engine.sync_engine,
                "connect",
                self._enable_sqlite_foreign_keys,
            )
        self.sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def init(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            organization_columns = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]
                    for item in inspect(sync_connection).get_columns("organizations")
                }
            )
            for column_name in (
                "archived_from_lifecycle_state",
                "archived_from_provisioning_state",
                "archived_from_subscription_state",
            ):
                if column_name not in organization_columns:
                    await connection.execute(text(
                        "ALTER TABLE organizations "
                        f"ADD COLUMN {column_name} VARCHAR(32) DEFAULT '' NOT NULL"
                    ))
            contact_columns = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]
                    for item in inspect(sync_connection).get_columns(
                        "organization_contacts"
                    )
                }
            )
            if "phone" not in contact_columns:
                await connection.execute(text(
                    "ALTER TABLE organization_contacts "
                    "ADD COLUMN phone VARCHAR(64) DEFAULT '' NOT NULL"
                ))
            managed_request_columns = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]
                    for item in inspect(sync_connection).get_columns(
                        "managed_access_requests"
                    )
                }
            )
            if "terms_version" not in managed_request_columns:
                await connection.execute(text(
                    "ALTER TABLE managed_access_requests "
                    "ADD COLUMN terms_version VARCHAR(32) DEFAULT '' NOT NULL"
                ))
            if "terms_acknowledged_at" not in managed_request_columns:
                timestamp_type = (
                    "TIMESTAMP WITH TIME ZONE"
                    if self.engine.dialect.name == "postgresql"
                    else "DATETIME"
                )
                await connection.execute(text(
                    "ALTER TABLE managed_access_requests "
                    f"ADD COLUMN terms_acknowledged_at {timestamp_type}"
                ))
            audit_columns = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]
                    for item in inspect(sync_connection).get_columns(
                        "control_plane_audit_events"
                    )
                }
            )
            if "retention_hold" not in audit_columns:
                boolean_type = (
                    "BOOLEAN"
                    if self.engine.dialect.name == "postgresql"
                    else "INTEGER"
                )
                await connection.execute(text(
                    "ALTER TABLE control_plane_audit_events "
                    f"ADD COLUMN retention_hold {boolean_type} "
                    "DEFAULT FALSE NOT NULL"
                ))
            enrollment_campaign_columns = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]
                    for item in inspect(sync_connection).get_columns(
                        "enrollment_campaigns"
                    )
                }
            )
            if "token_generation" not in enrollment_campaign_columns:
                await connection.execute(text(
                    "ALTER TABLE enrollment_campaigns "
                    "ADD COLUMN token_generation VARCHAR(36) DEFAULT '' NOT NULL"
                ))
            device_credential_columns = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]
                    for item in inspect(sync_connection).get_columns(
                        "device_credentials"
                    )
                }
            )
            if "authorized_user_id" not in device_credential_columns:
                await connection.execute(text(
                    "ALTER TABLE device_credentials "
                    "ADD COLUMN authorized_user_id VARCHAR(36)"
                ))
            if "functionality_release" not in device_credential_columns:
                await connection.execute(text(
                    "ALTER TABLE device_credentials "
                    "ADD COLUMN functionality_release INTEGER DEFAULT 0 NOT NULL"
                ))
            if "reauth_requested_at" not in device_credential_columns:
                timestamp_type = (
                    "TIMESTAMP WITH TIME ZONE"
                    if self.engine.dialect.name == "postgresql"
                    else "DATETIME"
                )
                await connection.execute(text(
                    "ALTER TABLE device_credentials "
                    f"ADD COLUMN reauth_requested_at {timestamp_type}"
                ))
            await connection.execute(text(
                "CREATE INDEX IF NOT EXISTS "
                "idx_device_credentials_authorized_user_id "
                "ON device_credentials (authorized_user_id)"
            ))
            await connection.execute(text(
                "CREATE INDEX IF NOT EXISTS "
                "idx_control_plane_audit_events_created_at "
                "ON control_plane_audit_events (created_at)"
            ))
            billing_notification_columns = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]
                    for item in inspect(sync_connection).get_columns(
                        "billing_notifications"
                    )
                }
            )
            if "deadline_at" not in billing_notification_columns:
                timestamp_type = (
                    "TIMESTAMP WITH TIME ZONE"
                    if self.engine.dialect.name == "postgresql"
                    else "DATETIME"
                )
                await connection.execute(text(
                    "ALTER TABLE billing_notifications "
                    f"ADD COLUMN deadline_at {timestamp_type}"
                ))
            columns = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]
                    for item in inspect(sync_connection).get_columns(
                        "active_video_streams"
                    )
                }
            )
            if "timezone_name" not in columns:
                await connection.execute(text(
                    "ALTER TABLE active_video_streams "
                    "ADD COLUMN timezone_name VARCHAR(64) "
                    "DEFAULT 'UTC' NOT NULL"
                ))
            if "device_name" not in columns:
                await connection.execute(text(
                    "ALTER TABLE active_video_streams "
                    "ADD COLUMN device_name VARCHAR(160) "
                    "DEFAULT 'Unknown device' NOT NULL"
                ))
            if "media_kind" not in columns:
                await connection.execute(text(
                    "ALTER TABLE active_video_streams "
                    "ADD COLUMN media_kind VARCHAR(16) "
                    "DEFAULT 'live' NOT NULL"
                ))
            if "recorded_at" not in columns:
                timestamp_type = (
                    "TIMESTAMP WITH TIME ZONE"
                    if self.engine.dialect.name == "postgresql"
                    else "DATETIME"
                )
                await connection.execute(text(
                    "ALTER TABLE active_video_streams "
                    f"ADD COLUMN recorded_at {timestamp_type}"
                ))
            if "duration_ms" not in columns:
                await connection.execute(text(
                    "ALTER TABLE active_video_streams "
                    "ADD COLUMN duration_ms BIGINT DEFAULT 0 NOT NULL"
                ))
            if "thumbnail_revision" not in columns:
                await connection.execute(text(
                    "ALTER TABLE active_video_streams "
                    "ADD COLUMN thumbnail_revision VARCHAR(64) "
                    "DEFAULT '' NOT NULL"
                ))
            if "remote_control_enabled" not in columns:
                await connection.execute(text(
                    "ALTER TABLE active_video_streams "
                    "ADD COLUMN remote_control_enabled BOOLEAN "
                    "DEFAULT FALSE NOT NULL"
                ))
            request_columns = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]
                    for item in inspect(sync_connection).get_columns(
                        "video_stream_requests"
                    )
                }
            )
            timestamp_type = (
                "TIMESTAMP WITH TIME ZONE"
                if self.engine.dialect.name == "postgresql"
                else "DATETIME"
            )
            if "started_at" not in request_columns:
                await connection.execute(text(
                    "ALTER TABLE video_stream_requests "
                    f"ADD COLUMN started_at {timestamp_type}"
                ))
            if "status_message" not in request_columns:
                await connection.execute(text(
                    "ALTER TABLE video_stream_requests "
                    "ADD COLUMN status_message VARCHAR(400) DEFAULT '' NOT NULL"
                ))
            if "remote_control_enabled" not in request_columns:
                await connection.execute(text(
                    "ALTER TABLE video_stream_requests "
                    "ADD COLUMN remote_control_enabled BOOLEAN "
                    "DEFAULT FALSE NOT NULL"
                ))
            for column_name in (
                "quality_source_width",
                "quality_source_height",
                "quality_source_fps_milli",
            ):
                if column_name not in request_columns:
                    await connection.execute(text(
                        "ALTER TABLE video_stream_requests "
                        f"ADD COLUMN {column_name} INTEGER DEFAULT 0 NOT NULL"
                    ))
            for column_name in (
                "audio_bytes_sent",
                "audio_bytes_received",
                "video_bytes_received",
            ):
                if column_name not in request_columns:
                    await connection.execute(text(
                        "ALTER TABLE video_stream_requests "
                        f"ADD COLUMN {column_name} BIGINT DEFAULT 0 NOT NULL"
                    ))
        async with self.sessions() as session:
            organizations = (await session.scalars(select(Organization))).all()
            for organization in organizations:
                legacy_hostname = (
                    f"{organization.designator.lower()}.r2c-tracker.com"
                )
                if organization.hostname.lower() == legacy_hostname:
                    organization.hostname = (
                        f"r2c-tracker.com/{organization.designator.lower()}"
                    )
                if organization.lifecycle_state != "archived":
                    organization.lifecycle_state = "extended_beta"
                    organization.billing_mode = "extended beta"
                    organization.trial_starts_at = None
                    organization.trial_ends_at = None
                elif organization.archived_from_lifecycle_state in {
                    "trial", "grace", "funded"
                }:
                    organization.archived_from_lifecycle_state = "extended_beta"
            subscriptions = (await session.scalars(select(Subscription))).all()
            for subscription in subscriptions:
                if subscription.state != "archived":
                    subscription.state = "extended_beta"
                    subscription.collection_method = "none"
                    subscription.billing_cadence = "calendar month allowance"
                    subscription.trial_starts_at = None
                    subscription.trial_ends_at = None
            await session.execute(
                update(BillingNotification)
                .where(
                    BillingNotification.state == "pending",
                    BillingNotification.notification_type.in_((
                        "funding_exhausted",
                        "trial_ending_7d",
                        "trial_ending_1d",
                        "trial_ended",
                        "grace_ending_7d",
                        "grace_ending_1d",
                        "grace_ended",
                    )),
                )
                .values(state="canceled")
            )
            subscribed_ids = set(
                await session.scalars(select(Subscription.organization_id))
            )
            for organization in organizations:
                if organization.id in subscribed_ids:
                    continue
                session.add(
                    Subscription(
                        id=new_id(),
                        organization_id=organization.id,
                        state=(
                            "archived"
                            if organization.lifecycle_state == "archived"
                            else "extended_beta"
                        ),
                        collection_method="none",
                        billing_cadence="calendar month allowance",
                        trial_starts_at=None,
                        trial_ends_at=None,
                        created_at=organization.created_at,
                        updated_at=utc_now(),
                    )
                )
            await session.commit()

    async def _notify_video_stream_change(
        self,
        session: AsyncSession,
        organization_id: str,
    ) -> None:
        if self.engine.dialect.name == "postgresql":
            await session.execute(
                select(func.pg_notify("r2c_stream_change", organization_id))
            )

    async def notify_video_thumbnail_preview(
        self,
        *,
        organization_id: str,
        device_credential_id: str,
        ttl_seconds: int,
    ) -> bool:
        """Ask the instance holding a tablet socket to refresh previews."""
        safe_ttl = max(10, min(int(ttl_seconds), 60))
        async with self.sessions() as session:
            credential = await session.scalar(
                select(DeviceCredential)
                .where(DeviceCredential.id == device_credential_id)
                .with_for_update()
            )
            if (
                credential is None
                or credential.organization_id != organization_id
                or credential.state != "active"
            ):
                return False
            if self.engine.dialect.name != "postgresql":
                return False
            await session.execute(
                select(
                    func.pg_notify(
                        "r2c_video_thumbnail_preview",
                        json.dumps(
                            {
                                "organizationId": organization_id,
                                "deviceCredentialId": device_credential_id,
                                "ttlSec": safe_ttl,
                            },
                            separators=(",", ":"),
                        ),
                    )
                )
            )
            await session.commit()
            return True

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> None:
        """Verify that the control-plane database can serve a simple query."""
        async with self.sessions() as session:
            await session.execute(select(1))

    async def deployment_activity(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Return privacy-minimal counts used by the guarded release workflow."""
        checked_at = now or utc_now()
        active_request_states = (
            "pending",
            "probing",
            "awaiting_approval",
            "approved",
            "streaming",
        )
        async with self.sessions() as session:
            active_streams = await session.scalar(
                select(func.count(ActiveVideoStream.id)).where(
                    ActiveVideoStream.state == "active",
                    ActiveVideoStream.expires_at >= checked_at,
                )
            )
            active_requests = await session.scalar(
                select(func.count(VideoStreamRequest.id)).where(
                    VideoStreamRequest.state.in_(active_request_states),
                    VideoStreamRequest.expires_at >= checked_at,
                )
            )
        return {
            "active_video_streams": int(active_streams or 0),
            "active_video_requests": int(active_requests or 0),
        }

    async def deployment_activity_details(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, list[dict]]:
        """Describe active video use for the authenticated release owner."""
        checked_at = as_utc(now or utc_now())
        active_request_states = (
            "pending",
            "probing",
            "awaiting_approval",
            "approved",
            "streaming",
        )
        async with self.sessions() as session:
            stream_rows = (await session.execute(
                select(ActiveVideoStream, DeviceCredential, Organization)
                .join(
                    DeviceCredential,
                    DeviceCredential.id == ActiveVideoStream.device_credential_id,
                )
                .join(Organization, Organization.id == ActiveVideoStream.organization_id)
                .where(
                    ActiveVideoStream.state == "active",
                    ActiveVideoStream.expires_at >= checked_at,
                )
                .order_by(
                    Organization.designator,
                    DeviceCredential.device_name,
                    ActiveVideoStream.drone_designator,
                )
            )).all()
            request_rows = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream, Organization)
                .join(
                    ActiveVideoStream,
                    ActiveVideoStream.id == VideoStreamRequest.active_stream_id,
                )
                .join(Organization, Organization.id == VideoStreamRequest.organization_id)
                .where(
                    VideoStreamRequest.state.in_(active_request_states),
                    VideoStreamRequest.expires_at >= checked_at,
                )
                .order_by(
                    Organization.designator,
                    VideoStreamRequest.requester_email,
                    VideoStreamRequest.requested_at,
                )
            )).all()

        grouped_streams: dict[tuple[str, str, str, str], dict] = {}
        for stream, credential, organization in stream_rows:
            key = (
                organization.designator,
                credential.id,
                stream.device_name,
                stream.incident_name,
            )
            detail = grouped_streams.setdefault(key, {
                "organization": organization.designator,
                "device": stream.device_name,
                "platform": credential.platform,
                "device_credential_id": credential.id,
                "incident": stream.incident_name,
                "stream_count": 0,
                "streams": [],
                "last_seen_at": as_utc(stream.last_seen_at).isoformat(),
                "expires_at": as_utc(stream.expires_at).isoformat(),
            })
            detail["stream_count"] += 1
            detail["streams"].append({
                "drone": stream.drone_designator,
                "media_kind": stream.media_kind,
                "session_id": stream.session_id,
            })
            detail["last_seen_at"] = max(
                detail["last_seen_at"],
                as_utc(stream.last_seen_at).isoformat(),
            )
            detail["expires_at"] = max(
                detail["expires_at"],
                as_utc(stream.expires_at).isoformat(),
            )

        return {
            "active_video_streams": list(grouped_streams.values()),
            "active_video_requests": [
                {
                    "organization": organization.designator,
                    "requester": request.requester_email,
                    "device": stream.device_name,
                    "drone": stream.drone_designator,
                    "state": request.state,
                    "request_id": request.id,
                    "expires_at": as_utc(request.expires_at).isoformat(),
                }
                for request, stream, organization in request_rows
            ],
        }

    async def ensure_platform_admin(
        self,
        *,
        email: str,
        display_name: str,
        bootstrap_password: str,
    ) -> PlatformAdminRecord:
        clean_email = normalize_email(email)
        clean_name = display_name.strip()
        if not clean_name:
            raise InvalidOrganizationError("Enter the platform administrator's name.")
        async with self.sessions() as session:
            existing = await session.scalar(
                select(PlatformAdminUser).where(
                    PlatformAdminUser.email == clean_email
                )
            )
            if existing is None:
                any_admin = await session.scalar(select(PlatformAdminUser.id))
                if any_admin is not None:
                    raise ControlPlaneError(
                        "A platform administrator already exists; invite additional "
                        "administrators from the authenticated site."
                    )
                existing = PlatformAdminUser(
                    id=new_id(),
                    email=clean_email,
                    display_name=clean_name,
                    password_hash=hash_password(bootstrap_password),
                    state="active",
                )
                session.add(existing)
                await session.commit()
            return PlatformAdminRecord(
                id=existing.id,
                email=existing.email,
                display_name=existing.display_name,
                state=existing.state,
            )

    async def reconcile_platform_admin_identity(
        self,
        *,
        email: str,
        display_name: str,
    ) -> PlatformAdminRecord:
        """Make the infrastructure-selected identity the sole active administrator."""
        clean_email = normalize_email(email)
        clean_name = display_name.strip()
        if not clean_name:
            raise InvalidOrganizationError("Enter the platform administrator's name.")
        async with self.sessions() as session:
            users = (await session.scalars(select(PlatformAdminUser))).all()
            target = next((user for user in users if user.email == clean_email), None)
            target_was_active = target is not None and target.state == "active"
            for user in users:
                if user.email != clean_email and user.state == "active":
                    user.state = "disabled"
            if target is None:
                target = PlatformAdminUser(
                    id=new_id(),
                    email=clean_email,
                    display_name=clean_name,
                    password_hash="",
                    state="active",
                )
                session.add(target)
            else:
                target.display_name = clean_name
                if not target_was_active:
                    # Never restore a former administrator's password when an
                    # infrastructure maintainer assigns the identity again.
                    target.password_hash = ""
                target.state = "active"
            await session.commit()
            return PlatformAdminRecord(
                id=target.id,
                email=target.email,
                display_name=target.display_name,
                state=target.state,
            )

    async def get_platform_admin(
        self,
        user_id: str,
    ) -> Optional[PlatformAdminRecord]:
        async with self.sessions() as session:
            user = await session.get(PlatformAdminUser, user_id)
        if user is None or user.state != "active":
            return None
        return PlatformAdminRecord(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            state=user.state,
        )

    async def authenticate_platform_admin(
        self,
        email: str,
        password: str,
        now: Optional[datetime] = None,
    ) -> Optional[PlatformAdminRecord]:
        clean_email = normalize_email(email)
        login_at = now or utc_now()
        async with self.sessions() as session:
            throttle = await session.scalar(
                select(PlatformAdminLoginThrottle).where(
                    PlatformAdminLoginThrottle.email == clean_email
                )
            )
            if (
                throttle is not None
                and as_utc(throttle.locked_until) is not None
                and as_utc(throttle.locked_until) > login_at
            ):
                verify_password(password, DUMMY_PASSWORD_HASH)
                return None
            user = await session.scalar(
                select(PlatformAdminUser).where(
                    PlatformAdminUser.email == clean_email,
                    PlatformAdminUser.state == "active",
                )
            )
            password_valid = (
                verify_password(password, user.password_hash)
                if user is not None
                else verify_password(password, DUMMY_PASSWORD_HASH)
            )
            if user is None or not password_valid:
                if throttle is None:
                    throttle = PlatformAdminLoginThrottle(
                        id=new_id(),
                        email=clean_email,
                        failure_count=0,
                        window_started_at=login_at,
                    )
                    session.add(throttle)
                window_started_at = as_utc(throttle.window_started_at) or login_at
                if window_started_at < login_at - timedelta(minutes=15):
                    throttle.failure_count = 0
                    throttle.window_started_at = login_at
                throttle.failure_count += 1
                if throttle.failure_count >= 5:
                    throttle.locked_until = login_at + timedelta(minutes=15)
                await session.commit()
                return None
            if throttle is not None:
                throttle.failure_count = 0
                throttle.window_started_at = login_at
                throttle.locked_until = None
            user.last_login_at = login_at
            await session.commit()
            return PlatformAdminRecord(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                state=user.state,
            )

    async def change_platform_admin_password(
        self,
        *,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> None:
        async with self.sessions() as session:
            user = await session.get(PlatformAdminUser, user_id)
            if (
                user is None
                or user.state != "active"
                or not verify_password(current_password, user.password_hash)
            ):
                raise ControlPlaneError("Current password is incorrect.")
            user.password_hash = hash_password(new_password)
            await session.commit()

    async def platform_admin_has_password(self, user_id: str) -> bool:
        async with self.sessions() as session:
            user = await session.get(PlatformAdminUser, user_id)
            return bool(
                user is not None
                and user.state == "active"
                and user.password_hash
            )

    async def issue_platform_admin_password_setup(
        self,
        *,
        email: str,
        identity_generation: str,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        clean_email = normalize_email(email)
        issued_at = now or utc_now()
        async with self.sessions() as session:
            user = await session.scalar(
                select(PlatformAdminUser).where(
                    PlatformAdminUser.email == clean_email,
                    PlatformAdminUser.state == "active",
                )
            )
            if user is None:
                return None
            throttle = await session.scalar(
                select(PlatformAdminPasswordSetupThrottle).where(
                    PlatformAdminPasswordSetupThrottle.email == clean_email
                )
            )
            if throttle is None:
                throttle = PlatformAdminPasswordSetupThrottle(
                    id=new_id(),
                    email=clean_email,
                    request_count=0,
                    window_started_at=issued_at,
                )
                session.add(throttle)
            window_started = as_utc(throttle.window_started_at) or issued_at
            last_requested = as_utc(throttle.last_requested_at)
            if window_started < issued_at - timedelta(hours=1):
                throttle.window_started_at = issued_at
                throttle.request_count = 0
            elif throttle.request_count >= 5:
                return None
            if (
                last_requested is not None
                and last_requested > issued_at - timedelta(minutes=1)
            ):
                return None
            existing_tokens = (
                await session.scalars(
                    select(PlatformAdminPasswordSetupToken).where(
                        PlatformAdminPasswordSetupToken.email == clean_email,
                        PlatformAdminPasswordSetupToken.consumed_at.is_(None),
                    )
                )
            ).all()
            for existing in existing_tokens:
                existing.consumed_at = issued_at
            token = secrets.token_urlsafe(32)
            session.add(
                PlatformAdminPasswordSetupToken(
                    id=new_id(),
                    email=clean_email,
                    token_hash=device_token_hash(token),
                    identity_generation=identity_generation,
                    created_at=issued_at,
                    expires_at=issued_at + timedelta(minutes=5),
                )
            )
            throttle.request_count += 1
            throttle.last_requested_at = issued_at
            await session.commit()
            return token

    async def set_platform_admin_password_from_token(
        self,
        *,
        token: str,
        email: str,
        identity_generation: str,
        new_password: str,
        now: Optional[datetime] = None,
    ) -> Optional[PlatformAdminRecord]:
        password_hash = hash_password(new_password)
        clean_email = normalize_email(email)
        consumed_at = now or utc_now()
        async with self.sessions() as session:
            setup = await session.scalar(
                select(PlatformAdminPasswordSetupToken).where(
                    PlatformAdminPasswordSetupToken.token_hash
                    == device_token_hash(token),
                    PlatformAdminPasswordSetupToken.email == clean_email,
                    PlatformAdminPasswordSetupToken.identity_generation
                    == identity_generation,
                    PlatformAdminPasswordSetupToken.consumed_at.is_(None),
                )
            )
            if (
                setup is None
                or (as_utc(setup.expires_at) or consumed_at) <= consumed_at
            ):
                return None
            user = await session.scalar(
                select(PlatformAdminUser).where(
                    PlatformAdminUser.email == clean_email,
                    PlatformAdminUser.state == "active",
                )
            )
            if user is None:
                return None
            setup.consumed_at = consumed_at
            user.password_hash = password_hash
            await session.commit()
            return PlatformAdminRecord(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                state=user.state,
            )

    async def create_organization(
        self,
        *,
        legal_name: str,
        designator: str,
        admin_name: str,
        admin_email: str,
        postal_address: str,
        actor_id: str,
        simulation: bool = True,
        now: Optional[datetime] = None,
        admin_phone: str = "",
    ) -> OrganizationRecord:
        created_at = now or utc_now()
        clean_name = legal_name.strip()
        clean_admin_name = admin_name.strip()
        clean_designator = normalize_designator(designator)
        clean_email = normalize_email(admin_email)
        clean_phone = " ".join(admin_phone.split())
        if not clean_name or len(clean_name) > 200:
            raise InvalidOrganizationError("Enter the organization's official name.")
        if not clean_admin_name or len(clean_admin_name) > 160:
            raise InvalidOrganizationError("Enter the site administrator's name.")
        if len(clean_phone) > 64:
            raise InvalidOrganizationError("Enter a phone number no longer than 64 characters.")

        hostname = f"r2c-tracker.com/{clean_designator.lower()}"
        organization = Organization(
            id=new_id(),
            legal_name=clean_name,
            designator=clean_designator,
            hostname=hostname,
            lifecycle_state="extended_beta",
            provisioning_state=(
                "simulation ready" if simulation else "provisioning queued"
            ),
            billing_mode="extended beta",
            trial_starts_at=None,
            trial_ends_at=None,
            notification_email=clean_email,
            created_at=created_at,
            updated_at=created_at,
        )
        contact = OrganizationContact(
            organization_id=organization.id,
            name=clean_admin_name,
            email=clean_email,
            phone=clean_phone,
            postal_address=postal_address.strip(),
            created_at=created_at,
        )
        owner = OrganizationUser(
            id=new_id(),
            organization_id=organization.id,
            email=clean_email,
            display_name=clean_admin_name,
            state="invited",
            activation_expires_at=created_at + timedelta(days=7),
            created_at=created_at,
        )
        owner.set_roles(DEFAULT_OWNER_ROLES)
        job = ProvisioningJob(
            organization_id=organization.id,
            state="simulated" if simulation else "queued",
            current_step=ONBOARDING_STEPS[-1] if simulation else ONBOARDING_STEPS[0],
            steps_json=json.dumps(
                [
                    {"step": step, "state": "simulated" if simulation else "queued"}
                    for step in ONBOARDING_STEPS
                ]
            ),
            simulation=simulation,
            created_at=created_at,
            completed_at=created_at if simulation else None,
        )
        subscription = Subscription(
            id=new_id(),
            organization_id=organization.id,
            state="extended_beta",
            collection_method="none",
            billing_cadence="calendar month allowance",
            trial_starts_at=None,
            trial_ends_at=None,
            created_at=created_at,
            updated_at=created_at,
        )
        audit = ControlPlaneAuditEvent(
            organization_id=organization.id,
            actor_type="platform_admin",
            actor_id=actor_id,
            event_type="organization.created",
            details_json=json.dumps(
                {
                    "designator": clean_designator,
                    "hostname": hostname,
                    "simulation": simulation,
                }
            ),
            created_at=created_at,
        )
        async with self.sessions() as session:
            existing = await session.scalar(
                select(Organization.id).where(
                    (Organization.designator == clean_designator)
                    | (Organization.hostname == hostname)
                )
            )
            if existing:
                raise DuplicateOrganizationError(
                    f"Preferred designator {clean_designator} is already in use. "
                    "Designators remain reserved after an organization is archived."
                )
            try:
                session.add(organization)
                await session.flush()
                session.add_all((contact, owner, job, subscription, audit))
                await session.execute(
                    update(ManagedAccessRequest)
                    .where(
                        ManagedAccessRequest.designator == clean_designator,
                        ManagedAccessRequest.state == "pending",
                    )
                    .values(state="organization created", updated_at=created_at)
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateOrganizationError(
                    f"Preferred designator {clean_designator} is already in use. "
                    "Designators remain reserved after an organization is archived."
                ) from exc

        return OrganizationRecord(
            id=organization.id,
            legal_name=organization.legal_name,
            designator=organization.designator,
            hostname=organization.hostname,
            lifecycle_state=organization.lifecycle_state,
            provisioning_state=organization.provisioning_state,
            billing_mode=organization.billing_mode,
            trial_ends_at=organization.trial_ends_at,
            records_visibility=organization.records_visibility,
            record_retention_days=organization.record_retention_days,
            log_retention_days=organization.log_retention_days,
            notification_email=organization.notification_email,
            primary_admin_name=contact.name,
            primary_admin_email=contact.email,
            subscription_state=subscription.state,
            credit_balance=Decimal("0.00"),
            primary_admin_postal_address=contact.postal_address,
            primary_admin_phone=contact.phone,
        )

    async def mark_organization_invitation_sent(
        self,
        *,
        organization_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> None:
        sent_at = now or utc_now()
        async with self.sessions() as session:
            organization = await session.get(Organization, organization_id)
            job = await session.scalar(
                select(ProvisioningJob).where(
                    ProvisioningJob.organization_id == organization_id
                )
            )
            if organization is None or job is None:
                raise ControlPlaneError("Organization provisioning job not found.")
            organization.provisioning_state = "activation pending"
            organization.updated_at = sent_at
            steps = [
                {"step": step, "state": "completed"}
                for step in ONBOARDING_STEPS[:-1]
            ] + [{"step": ONBOARDING_STEPS[-1], "state": "pending"}]
            job.state = "waiting for activation"
            # Sending a real invitation is the explicit promotion seam for an
            # organization originally created while the control plane was in
            # simulation mode.
            job.simulation = False
            job.current_step = ONBOARDING_STEPS[-1]
            job.steps_json = json.dumps(steps)
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="platform_admin",
                    actor_id=actor_id,
                    event_type="administrator.invitation_sent",
                    details_json="{}",
                    created_at=sent_at,
                )
            )
            await session.commit()

    async def mark_organization_access_email_sent(
        self,
        *,
        organization_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> None:
        sent_at = now or utc_now()
        async with self.sessions() as session:
            organization = await session.get(Organization, organization_id)
            if organization is None or organization.lifecycle_state == "archived":
                raise ControlPlaneError("Active organization not found.")
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="platform_admin",
                    actor_id=actor_id,
                    event_type="administrator.access_email_sent",
                    details_json="{}",
                    created_at=sent_at,
                )
            )
            await session.commit()

    async def _complete_organization_activation(
        self,
        session: AsyncSession,
        organization_id: str,
        activated_at: datetime,
    ) -> None:
        organization = await session.get(Organization, organization_id)
        if organization is None:
            raise ControlPlaneError("Organization not found.")
        first_activation = organization.provisioning_state != "ready"
        organization.provisioning_state = "ready"
        if first_activation:
            organization.lifecycle_state = "extended_beta"
            organization.billing_mode = "extended beta"
            organization.trial_starts_at = None
            organization.trial_ends_at = None
        organization.updated_at = activated_at
        subscription = await session.scalar(
            select(Subscription).where(
                Subscription.organization_id == organization_id
            )
        )
        if subscription is not None and first_activation:
            subscription.state = "extended_beta"
            subscription.collection_method = "none"
            subscription.billing_cadence = "calendar month allowance"
            subscription.trial_starts_at = None
            subscription.trial_ends_at = None
            subscription.updated_at = activated_at
        job = await session.scalar(
            select(ProvisioningJob).where(
                ProvisioningJob.organization_id == organization_id
            )
        )
        if job is not None and not job.simulation:
            job.state = "completed"
            job.current_step = ONBOARDING_STEPS[-1]
            job.steps_json = json.dumps(
                [{"step": step, "state": "completed"} for step in ONBOARDING_STEPS]
            )
            job.completed_at = activated_at

    async def list_organizations(self) -> tuple[OrganizationRecord, ...]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(Organization, OrganizationContact, Subscription)
                    .join(
                        OrganizationContact,
                        OrganizationContact.organization_id == Organization.id,
                    )
                    .join(
                        Subscription,
                        Subscription.organization_id == Organization.id,
                    )
                    .where(OrganizationContact.contact_role == "primary_admin")
                    .order_by(Organization.designator)
                )
            ).all()
            ledger_rows = (
                await session.execute(
                    select(
                        BillingLedgerEntry.organization_id,
                        BillingLedgerEntry.amount,
                    )
                )
            ).all()
            usage_rows = (
                await session.execute(
                    select(
                        UsageDaily.organization_id,
                        UsageDaily.compute_cost,
                        UsageDaily.network_cost,
                        UsageDaily.storage_cost,
                        UsageDaily.database_cost,
                        UsageDaily.faa_proxy_cost,
                        UsageDaily.turn_relay_cost,
                        UsageDaily.other_cost,
                    )
                )
            ).all()
        balances: dict[str, Decimal] = {}
        for organization_id, amount in ledger_rows:
            balances[organization_id] = (
                balances.get(organization_id, Decimal("0")) + Decimal(amount)
            )
        usage_costs: dict[str, Decimal] = {}
        for organization_id, *costs in usage_rows:
            usage_costs[organization_id] = (
                usage_costs.get(organization_id, Decimal("0"))
                + sum((Decimal(cost) for cost in costs), Decimal("0"))
            )
        return tuple(
            OrganizationRecord(
                id=organization.id,
                legal_name=organization.legal_name,
                designator=organization.designator,
                hostname=organization.hostname,
                lifecycle_state=organization.lifecycle_state,
                provisioning_state=organization.provisioning_state,
                billing_mode=organization.billing_mode,
                trial_ends_at=as_utc(organization.trial_ends_at),
                records_visibility=organization.records_visibility,
                record_retention_days=organization.record_retention_days,
                log_retention_days=organization.log_retention_days,
                notification_email=organization.notification_email,
                primary_admin_name=contact.name,
                primary_admin_email=contact.email,
                subscription_state=subscription.state,
                credit_balance=max(
                    balances.get(organization.id, Decimal("0.00"))
                    - usage_costs.get(organization.id, Decimal("0.00")),
                    Decimal("0.00"),
                ),
                primary_admin_postal_address=contact.postal_address,
                primary_admin_phone=contact.phone,
            )
            for organization, contact, subscription in rows
        )

    async def get_organization(
        self,
        designator: str,
        *,
        include_archived: bool = False,
    ) -> Optional[OrganizationRecord]:
        clean_designator = normalize_designator(designator)
        organizations = await self.list_organizations()
        return next(
            (
                organization
                for organization in organizations
                if organization.designator == clean_designator
                and (
                    include_archived
                    or organization.lifecycle_state != "archived"
                )
            ),
            None,
        )

    async def create_managed_access_request(
        self,
        *,
        requester_name: str,
        requester_email: str,
        requester_phone: str,
        organization_name: str,
        designator: str,
        source_host: str,
        terms_acknowledged: bool,
        terms_version: str,
        now: Optional[datetime] = None,
    ) -> ManagedAccessRequestRecord:
        submitted_at = now or utc_now()
        clean_name = requester_name.strip()
        clean_email = normalize_email(requester_email)
        clean_phone = " ".join(requester_phone.split())
        clean_organization = organization_name.strip()
        clean_designator = normalize_designator(designator)
        clean_source_host = source_host.strip().lower()[:255]
        clean_terms_version = terms_version.strip()
        if not clean_name or len(clean_name) > 160:
            raise InvalidOrganizationError("Enter a requester name.")
        if len(clean_phone) > 64:
            raise InvalidOrganizationError("Enter a phone number no longer than 64 characters.")
        if not clean_organization or len(clean_organization) > 200:
            raise InvalidOrganizationError("Enter the organization's official name.")
        if not terms_acknowledged:
            raise InvalidOrganizationError(
                "Acknowledge the managed-service best-effort safety terms before requesting access."
            )
        if clean_terms_version != MANAGED_ACCESS_TERMS_VERSION:
            raise InvalidOrganizationError(
                "Review and acknowledge the current managed-service best-effort safety terms."
            )
        async with self.sessions() as session:
            existing = await session.scalar(
                select(ManagedAccessRequest)
                .where(
                    ManagedAccessRequest.requester_email == clean_email,
                    ManagedAccessRequest.designator == clean_designator,
                    ManagedAccessRequest.state == "pending",
                    ManagedAccessRequest.submitted_at
                    >= submitted_at - timedelta(minutes=15),
                )
                .order_by(ManagedAccessRequest.submitted_at.desc())
            )
            if existing is None:
                existing = ManagedAccessRequest(
                    requester_name=clean_name,
                    requester_email=clean_email,
                    requester_phone=clean_phone,
                    organization_name=clean_organization,
                    designator=clean_designator,
                    source_host=clean_source_host,
                    terms_version=clean_terms_version,
                    terms_acknowledged_at=submitted_at,
                    submitted_at=submitted_at,
                    updated_at=submitted_at,
                )
                session.add(existing)
            else:
                existing.requester_name = clean_name
                existing.requester_phone = clean_phone
                existing.organization_name = clean_organization
                existing.source_host = clean_source_host
                existing.terms_version = clean_terms_version
                existing.terms_acknowledged_at = submitted_at
                existing.updated_at = submitted_at
            await session.commit()
            return ManagedAccessRequestRecord(
                id=existing.id,
                requester_name=existing.requester_name,
                requester_email=existing.requester_email,
                requester_phone=existing.requester_phone,
                organization_name=existing.organization_name,
                designator=existing.designator,
                state=existing.state,
                source_host=existing.source_host,
                terms_version=existing.terms_version,
                terms_acknowledged_at=as_utc(existing.terms_acknowledged_at),
                submitted_at=as_utc(existing.submitted_at),
            )

    async def list_managed_access_requests(
        self,
    ) -> tuple[ManagedAccessRequestRecord, ...]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(ManagedAccessRequest).order_by(
                        ManagedAccessRequest.submitted_at.desc()
                    )
                )
            ).all()
        return tuple(
            ManagedAccessRequestRecord(
                id=row.id,
                requester_name=row.requester_name,
                requester_email=row.requester_email,
                requester_phone=row.requester_phone,
                organization_name=row.organization_name,
                designator=row.designator,
                state=row.state,
                source_host=row.source_host,
                terms_version=row.terms_version,
                terms_acknowledged_at=as_utc(row.terms_acknowledged_at),
                submitted_at=as_utc(row.submitted_at),
            )
            for row in rows
        )

    async def update_organization_administrator(
        self,
        *,
        designator: str,
        legal_name: str,
        admin_name: str,
        admin_email: str,
        admin_phone: str,
        postal_address: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> AdministratorUpdateRecord:
        clean_designator = normalize_designator(designator)
        clean_legal_name = legal_name.strip()
        clean_name = admin_name.strip()
        clean_email = normalize_email(admin_email)
        clean_phone = " ".join(admin_phone.split())
        clean_address = postal_address.strip()
        changed_at = now or utc_now()
        if not clean_legal_name or len(clean_legal_name) > 200:
            raise InvalidOrganizationError("Enter the organization's official name.")
        if not clean_name or len(clean_name) > 160:
            raise InvalidOrganizationError("Enter the site administrator's name.")
        if len(clean_phone) > 64:
            raise InvalidOrganizationError("Enter a phone number no longer than 64 characters.")
        if len(clean_address) > 2000:
            raise InvalidOrganizationError("Enter a postal address no longer than 2,000 characters.")
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(Organization, OrganizationContact)
                    .join(
                        OrganizationContact,
                        OrganizationContact.organization_id == Organization.id,
                    )
                    .where(
                        Organization.designator == clean_designator,
                        OrganizationContact.contact_role == "primary_admin",
                    )
                )
            ).first()
            if row is None:
                raise InvalidOrganizationError("Organization not found.")
            organization, contact = row
            if organization.lifecycle_state == "archived":
                raise ControlPlaneError("Unarchive the organization before editing its administrator.")
            old_name = contact.name
            old_email = contact.email
            old_phone = contact.phone
            administrator_changed = old_email != clean_email
            organization.legal_name = clean_legal_name
            organization.notification_email = clean_email
            organization.updated_at = changed_at
            contact.name = clean_name
            contact.email = clean_email
            contact.phone = clean_phone
            contact.postal_address = clean_address
            if administrator_changed:
                old_user = await session.scalar(
                    select(OrganizationUser).where(
                        OrganizationUser.organization_id == organization.id,
                        OrganizationUser.email == old_email,
                    )
                )
                if old_user is not None:
                    old_user.set_roles(())
                    old_user.state = "archived"
                    old_user.password_hash = ""
                new_user = await session.scalar(
                    select(OrganizationUser).where(
                        OrganizationUser.organization_id == organization.id,
                        OrganizationUser.email == clean_email,
                    )
                )
                if new_user is None:
                    new_user = OrganizationUser(
                        id=new_id(),
                        organization_id=organization.id,
                        email=clean_email,
                        display_name=clean_name,
                        state="invited",
                        activation_nonce=new_id(),
                        activation_expires_at=changed_at + timedelta(days=7),
                        created_at=changed_at,
                    )
                    session.add(new_user)
                else:
                    new_user.display_name = clean_name
                    new_user.state = "invited"
                    new_user.password_hash = ""
                    new_user.activation_nonce = new_id()
                    new_user.activation_expires_at = changed_at + timedelta(days=7)
                new_user.set_roles(DEFAULT_OWNER_ROLES)
            else:
                owner = await session.scalar(
                    select(OrganizationUser).where(
                        OrganizationUser.organization_id == organization.id,
                        OrganizationUser.email == clean_email,
                    )
                )
                if owner is not None:
                    owner.display_name = clean_name
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization.id,
                    actor_type="platform_admin",
                    actor_id=actor_id,
                    event_type=(
                        "administrator.replaced"
                        if administrator_changed
                        else "administrator.contact_updated"
                    ),
                    details_json=json.dumps({
                        "old_name": old_name,
                        "old_email": old_email,
                        "old_phone": old_phone,
                        "new_name": clean_name,
                        "new_email": clean_email,
                        "new_phone": clean_phone,
                    }),
                    created_at=changed_at,
                )
            )
            await session.execute(
                update(ManagedAccessRequest)
                .where(
                    ManagedAccessRequest.designator == clean_designator,
                    ManagedAccessRequest.state == "pending",
                )
                .values(state="organization created", updated_at=changed_at)
            )
            await session.commit()
        record = await self.get_organization(clean_designator)
        if record is None:
            raise ControlPlaneError("Updated organization could not be read.")
        return AdministratorUpdateRecord(
            organization=record,
            old_name=old_name,
            old_email=old_email,
            old_phone=old_phone,
            administrator_changed=administrator_changed,
        )

    async def archive_organization(
        self,
        *,
        designator: str,
        actor_id: str,
        administrator_contact: str,
        now: Optional[datetime] = None,
    ) -> OrganizationRecord:
        clean_designator = normalize_designator(designator)
        clean_contact = " ".join(administrator_contact.split())
        if len(clean_contact) < 10 or len(clean_contact) > 500:
            raise ControlPlaneError(
                "Record how and when the organization administrator was contacted."
            )
        archived_at = now or utc_now()
        async with self.sessions() as session:
            organization = await session.scalar(
                select(Organization).where(
                    Organization.designator == clean_designator
                )
            )
            if organization is None:
                raise InvalidOrganizationError("Organization not found.")
            if organization.lifecycle_state == "archived":
                raise ControlPlaneError(
                    f"{clean_designator} is already archived."
                )
            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.organization_id == organization.id
                )
            )
            organization.archived_from_lifecycle_state = organization.lifecycle_state
            organization.archived_from_provisioning_state = organization.provisioning_state
            organization.archived_from_subscription_state = (
                subscription.state if subscription is not None else ""
            )
            organization.lifecycle_state = "archived"
            organization.provisioning_state = "archived"
            organization.updated_at = archived_at
            await session.execute(
                update(OrganizationUser)
                .where(OrganizationUser.organization_id == organization.id)
                .values(state="archived")
            )
            await session.execute(
                update(OrganizationPasswordResetToken)
                .where(
                    OrganizationPasswordResetToken.organization_id
                    == organization.id,
                    OrganizationPasswordResetToken.consumed_at.is_(None),
                )
                .values(consumed_at=archived_at)
            )
            await session.execute(
                update(EnrollmentCampaign)
                .where(
                    EnrollmentCampaign.organization_id == organization.id,
                    EnrollmentCampaign.state == "active",
                )
                .values(state="revoked", revoked_at=archived_at)
            )
            await session.execute(
                update(DeviceCredential)
                .where(
                    DeviceCredential.organization_id == organization.id,
                    DeviceCredential.state == "active",
                )
                .values(state="revoked")
            )
            await session.execute(
                update(ActiveVideoStream)
                .where(
                    ActiveVideoStream.organization_id == organization.id,
                    ActiveVideoStream.state == "active",
                )
                .values(state="stopped", expires_at=archived_at)
            )
            await session.execute(
                update(VideoStreamRequest)
                .where(
                    VideoStreamRequest.organization_id == organization.id,
                    VideoStreamRequest.state.not_in(("stopped", "expired")),
                )
                .values(state="stopped", stopped_at=archived_at)
            )
            if subscription is not None:
                subscription.state = "archived"
                subscription.updated_at = archived_at
            job = await session.scalar(
                select(ProvisioningJob).where(
                    ProvisioningJob.organization_id == organization.id
                )
            )
            if job is not None:
                job.state = "archived"
                job.current_step = "organization archived"
                job.completed_at = archived_at
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization.id,
                    actor_type="platform_admin",
                    actor_id=actor_id,
                    event_type="organization.archived",
                    details_json=json.dumps(
                        {
                            "designator": clean_designator,
                            "hostname": organization.hostname,
                            "previous_lifecycle_state": organization.archived_from_lifecycle_state,
                            "previous_provisioning_state": organization.archived_from_provisioning_state,
                            "previous_subscription_state": organization.archived_from_subscription_state,
                            "administrator_contact": clean_contact,
                        }
                    ),
                    created_at=archived_at,
                )
            )
            await session.commit()
        record = await self.get_organization(
            clean_designator,
            include_archived=True,
        )
        if record is None:
            raise ControlPlaneError("Archived organization could not be read.")
        return record

    async def unarchive_organization(
        self,
        *,
        designator: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> OrganizationRecord:
        clean_designator = normalize_designator(designator)
        restored_at = now or utc_now()
        async with self.sessions() as session:
            organization = await session.scalar(
                select(Organization).where(
                    Organization.designator == clean_designator
                )
            )
            if organization is None:
                raise InvalidOrganizationError("Organization not found.")
            if organization.lifecycle_state != "archived":
                raise ControlPlaneError(f"{clean_designator} is not archived.")
            organization.lifecycle_state = "extended_beta"
            organization.billing_mode = "extended beta"
            organization.trial_starts_at = None
            organization.trial_ends_at = None
            organization.provisioning_state = (
                organization.archived_from_provisioning_state or "ready"
            )
            organization.updated_at = restored_at
            contact = await session.scalar(
                select(OrganizationContact).where(
                    OrganizationContact.organization_id == organization.id,
                    OrganizationContact.contact_role == "primary_admin",
                )
            )
            if contact is None:
                raise ControlPlaneError("Primary administrator contact not found.")
            owner = await session.scalar(
                select(OrganizationUser).where(
                    OrganizationUser.organization_id == organization.id,
                    OrganizationUser.email == contact.email,
                )
            )
            if owner is None:
                owner = OrganizationUser(
                    id=new_id(),
                    organization_id=organization.id,
                    email=contact.email,
                    display_name=contact.name,
                    created_at=restored_at,
                )
                session.add(owner)
            owner.display_name = contact.name
            owner.state = "invited"
            owner.password_hash = ""
            owner.activation_nonce = new_id()
            owner.activation_expires_at = restored_at + timedelta(days=7)
            owner.set_roles(DEFAULT_OWNER_ROLES)
            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.organization_id == organization.id
                )
            )
            if subscription is not None:
                subscription.state = "extended_beta"
                subscription.collection_method = "none"
                subscription.billing_cadence = "calendar month allowance"
                subscription.trial_starts_at = None
                subscription.trial_ends_at = None
                subscription.updated_at = restored_at
            job = await session.scalar(
                select(ProvisioningJob).where(
                    ProvisioningJob.organization_id == organization.id
                )
            )
            if job is not None:
                job.state = "waiting for activation"
                job.current_step = ONBOARDING_STEPS[-1]
                job.simulation = False
                job.completed_at = None
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization.id,
                    actor_type="platform_admin",
                    actor_id=actor_id,
                    event_type="organization.unarchived",
                    details_json=json.dumps({
                        "designator": clean_designator,
                        "credentials_restored": False,
                        "campaigns_restored": False,
                    }),
                    created_at=restored_at,
                )
            )
            await session.commit()
        record = await self.get_organization(clean_designator)
        if record is None:
            raise ControlPlaneError("Unarchived organization could not be read.")
        return record

    async def get_organization_by_id(
        self,
        organization_id: str,
    ) -> Optional[OrganizationRecord]:
        organizations = await self.list_organizations()
        return next(
            (
                organization
                for organization in organizations
                if organization.id == organization_id
            ),
            None,
        )

    async def get_organization_by_hostname(
        self,
        hostname: str,
    ) -> Optional[OrganizationRecord]:
        clean_hostname = hostname.strip().lower().rstrip(".")
        if not clean_hostname:
            return None
        organizations = await self.list_organizations()
        return next(
            (
                organization
                for organization in organizations
                if organization.hostname.lower() == clean_hostname
            ),
            None,
        )

    async def get_user(self, user_id: str) -> Optional[UserRecord]:
        async with self.sessions() as session:
            user = await session.get(OrganizationUser, user_id)
        if user is None:
            return None
        return UserRecord(
            id=user.id,
            organization_id=user.organization_id,
            email=user.email,
            display_name=user.display_name,
            roles=user.roles,
            state=user.state,
        )

    async def list_active_users_by_email(
        self,
        email: str,
    ) -> tuple[UserRecord, ...]:
        """Return every active organization membership for one identity."""
        clean_email = normalize_email(email)
        async with self.sessions() as session:
            users = (
                await session.scalars(
                    select(OrganizationUser)
                    .join(
                        Organization,
                        Organization.id == OrganizationUser.organization_id,
                    )
                    .where(
                        OrganizationUser.email == clean_email,
                        OrganizationUser.state == "active",
                        Organization.lifecycle_state != "archived",
                    )
                    .order_by(Organization.designator)
                )
            ).all()
        return tuple(
            UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )
            for user in users
        )

    async def list_active_users_by_external_identity(
        self,
        *,
        provider: str,
        issuer: str,
        subject: str,
    ) -> tuple[UserRecord, ...]:
        """Return active memberships explicitly bound to one OIDC identity."""
        async with self.sessions() as session:
            users = (
                await session.scalars(
                    select(OrganizationUser)
                    .join(
                        OrganizationExternalIdentity,
                        OrganizationExternalIdentity.user_id == OrganizationUser.id,
                    )
                    .join(
                        Organization,
                        Organization.id == OrganizationUser.organization_id,
                    )
                    .where(
                        OrganizationExternalIdentity.provider == provider,
                        OrganizationExternalIdentity.issuer == issuer,
                        OrganizationExternalIdentity.subject == subject,
                        OrganizationUser.state == "active",
                        Organization.lifecycle_state != "archived",
                    )
                    .order_by(Organization.designator)
                )
            ).all()
        return tuple(
            UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )
            for user in users
        )

    async def get_invitation(
        self,
        designator: str,
        email: str,
    ) -> Optional[InvitationRecord]:
        clean_designator = normalize_designator(designator)
        clean_email = normalize_email(email)
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(OrganizationUser, Organization)
                    .join(
                        Organization,
                        Organization.id == OrganizationUser.organization_id,
                    )
                    .where(
                        Organization.designator == clean_designator,
                        OrganizationUser.email == clean_email,
                        OrganizationUser.state == "invited",
                    )
                )
            ).first()
        if row is None:
            return None
        user, organization = row
        expires_at = as_utc(user.activation_expires_at)
        if expires_at is None:
            return None
        return InvitationRecord(
            user_id=user.id,
            organization_id=user.organization_id,
            designator=organization.designator,
            email=user.email,
            activation_nonce=user.activation_nonce,
            expires_at=expires_at,
        )

    async def renew_invitation(
        self,
        designator: str,
        email: str,
        now: Optional[datetime] = None,
    ) -> InvitationRecord:
        clean_designator = normalize_designator(designator)
        clean_email = normalize_email(email)
        issued_at = now or utc_now()
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(OrganizationUser, Organization)
                    .join(
                        Organization,
                        Organization.id == OrganizationUser.organization_id,
                    )
                    .where(
                        Organization.designator == clean_designator,
                        OrganizationUser.email == clean_email,
                        OrganizationUser.state == "invited",
                    )
                )
            ).first()
            if row is None:
                raise ControlPlaneError(
                    "No pending administrator invitation is available."
                )
            user, organization = row
            user.activation_nonce = new_id()
            user.activation_expires_at = issued_at + timedelta(days=7)
            await session.commit()
            return InvitationRecord(
                user_id=user.id,
                organization_id=user.organization_id,
                designator=organization.designator,
                email=user.email,
                activation_nonce=user.activation_nonce,
                expires_at=as_utc(user.activation_expires_at),
            )

    async def renew_member_invitation(
        self,
        *,
        organization_id: str,
        user_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> UserRecord:
        issued_at = now or utc_now()
        async with self.sessions() as session:
            user = await session.get(OrganizationUser, user_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Member not found.")
            if "organization_owner" in user.roles:
                raise ControlPlaneError(
                    "The organization owner's invitation is managed from "
                    "platform administration."
                )
            if user.state != "invited":
                raise ControlPlaneError(
                    "Only a member pending activation can be sent an invitation."
                )
            user.activation_nonce = new_id()
            user.activation_expires_at = issued_at + timedelta(days=7)
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="organization_user",
                    actor_id=actor_id,
                    event_type="member.invitation_renewed",
                    details_json=json.dumps({"user_id": user.id}),
                    created_at=issued_at,
                )
            )
            await session.commit()
            return UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )

    async def mark_member_invitation_sent(
        self,
        *,
        organization_id: str,
        user_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> None:
        sent_at = now or utc_now()
        async with self.sessions() as session:
            user = await session.get(OrganizationUser, user_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Member not found.")
            if user.state != "invited":
                raise ControlPlaneError("Member is no longer pending activation.")
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="organization_user",
                    actor_id=actor_id,
                    event_type="member.invitation_sent",
                    details_json=json.dumps({"user_id": user.id}),
                    created_at=sent_at,
                )
            )
            await session.commit()

    async def authenticate_user(
        self,
        designator: str,
        email: str,
        password: str,
        now: Optional[datetime] = None,
    ) -> Optional[UserRecord]:
        clean_designator = normalize_designator(designator)
        clean_email = normalize_email(email)
        login_at = now or utc_now()
        async with self.sessions() as session:
            organization = await session.scalar(
                select(Organization).where(
                    Organization.designator == clean_designator
                )
            )
            if organization is None:
                verify_password(password, DUMMY_PASSWORD_HASH)
                return None
            throttle = await session.scalar(
                select(OrganizationLoginThrottle).where(
                    OrganizationLoginThrottle.organization_id == organization.id,
                    OrganizationLoginThrottle.email == clean_email,
                )
            )
            if (
                throttle is not None
                and as_utc(throttle.locked_until) is not None
                and as_utc(throttle.locked_until) > login_at
            ):
                verify_password(password, DUMMY_PASSWORD_HASH)
                return None
            user = await session.scalar(
                select(OrganizationUser).where(
                    OrganizationUser.organization_id == organization.id,
                    OrganizationUser.email == clean_email,
                    OrganizationUser.state == "active",
                )
            )
            password_valid = (
                verify_password(password, user.password_hash)
                if user is not None
                else verify_password(password, DUMMY_PASSWORD_HASH)
            )
            if user is None or not password_valid:
                if throttle is None:
                    throttle = OrganizationLoginThrottle(
                        id=new_id(),
                        organization_id=organization.id,
                        email=clean_email,
                        failure_count=0,
                        window_started_at=login_at,
                    )
                    session.add(throttle)
                window_started_at = as_utc(throttle.window_started_at) or login_at
                if window_started_at < login_at - timedelta(minutes=15):
                    throttle.failure_count = 0
                    throttle.window_started_at = login_at
                throttle.failure_count += 1
                if throttle.failure_count >= 5:
                    throttle.locked_until = login_at + timedelta(minutes=15)
                await session.commit()
                return None
            if throttle is not None:
                throttle.failure_count = 0
                throttle.window_started_at = login_at
                throttle.locked_until = None
            user.last_login_at = login_at
            await session.commit()
            return UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )

    async def issue_organization_password_reset(
        self,
        *,
        designator: str,
        email: str,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        clean_designator = normalize_designator(designator)
        clean_email = normalize_email(email)
        issued_at = now or utc_now()
        async with self.sessions() as session:
            organization = await session.scalar(
                select(Organization).where(
                    Organization.designator == clean_designator
                )
            )
            if organization is None:
                return None
            user = await session.scalar(
                select(OrganizationUser).where(
                    OrganizationUser.organization_id == organization.id,
                    OrganizationUser.email == clean_email,
                    OrganizationUser.state == "active",
                )
            )
            if user is None:
                return None
            throttle = await session.scalar(
                select(OrganizationPasswordResetThrottle).where(
                    OrganizationPasswordResetThrottle.organization_id
                    == organization.id,
                    OrganizationPasswordResetThrottle.email == clean_email,
                )
            )
            if throttle is None:
                throttle = OrganizationPasswordResetThrottle(
                    id=new_id(),
                    organization_id=organization.id,
                    email=clean_email,
                    request_count=0,
                    window_started_at=issued_at,
                )
                session.add(throttle)
            window_started = as_utc(throttle.window_started_at) or issued_at
            last_requested = as_utc(throttle.last_requested_at)
            if window_started < issued_at - timedelta(hours=1):
                throttle.window_started_at = issued_at
                throttle.request_count = 0
            elif throttle.request_count >= 5:
                return None
            if (
                last_requested is not None
                and last_requested > issued_at - timedelta(minutes=1)
            ):
                return None
            existing_tokens = (
                await session.scalars(
                    select(OrganizationPasswordResetToken).where(
                        OrganizationPasswordResetToken.organization_id
                        == organization.id,
                        OrganizationPasswordResetToken.user_id == user.id,
                        OrganizationPasswordResetToken.consumed_at.is_(None),
                    )
                )
            ).all()
            for existing in existing_tokens:
                existing.consumed_at = issued_at
            token = secrets.token_urlsafe(32)
            session.add(
                OrganizationPasswordResetToken(
                    id=new_id(),
                    organization_id=organization.id,
                    user_id=user.id,
                    token_hash=device_token_hash(token),
                    created_at=issued_at,
                    expires_at=issued_at + timedelta(minutes=15),
                )
            )
            throttle.request_count += 1
            throttle.last_requested_at = issued_at
            await session.commit()
            return token

    async def set_organization_password_from_reset(
        self,
        *,
        designator: str,
        token: str,
        new_password: str,
        now: Optional[datetime] = None,
    ) -> Optional[UserRecord]:
        clean_designator = normalize_designator(designator)
        password_hash = hash_password(new_password)
        consumed_at = now or utc_now()
        async with self.sessions() as session:
            reset = await session.scalar(
                select(OrganizationPasswordResetToken)
                .join(
                    Organization,
                    Organization.id
                    == OrganizationPasswordResetToken.organization_id,
                )
                .where(
                    Organization.designator == clean_designator,
                    OrganizationPasswordResetToken.token_hash
                    == device_token_hash(token),
                    OrganizationPasswordResetToken.consumed_at.is_(None),
                )
            )
            if (
                reset is None
                or (as_utc(reset.expires_at) or consumed_at) <= consumed_at
            ):
                return None
            user = await session.get(OrganizationUser, reset.user_id)
            if (
                user is None
                or user.organization_id != reset.organization_id
                or user.state != "active"
            ):
                return None
            reset.consumed_at = consumed_at
            user.password_hash = password_hash
            user.last_login_at = consumed_at
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=user.organization_id,
                    actor_type="organization_user",
                    actor_id=user.id,
                    event_type="administrator.password_reset",
                    details_json="{}",
                    created_at=consumed_at,
                )
            )
            await session.commit()
            return UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )

    async def authorize_google_user(
        self,
        designator: str,
        email: str,
        now: Optional[datetime] = None,
        *,
        activation_nonce: Optional[str] = None,
    ) -> Optional[UserRecord]:
        """Authorize an exact verified email and activate a live invitation."""
        clean_designator = normalize_designator(designator)
        clean_email = normalize_email(email)
        login_at = now or utc_now()
        async with self.sessions() as session:
            user = await session.scalar(
                select(OrganizationUser)
                .join(
                    Organization,
                    Organization.id == OrganizationUser.organization_id,
                )
                .where(
                    Organization.designator == clean_designator,
                    OrganizationUser.email == clean_email,
                    OrganizationUser.state.in_(("active", "invited")),
                )
            )
            if user is None:
                return None
            if user.state == "invited":
                if activation_nonce is not None and not secrets.compare_digest(
                    user.activation_nonce, activation_nonce
                ):
                    return None
                activation_expires_at = as_utc(user.activation_expires_at)
                if (
                    activation_expires_at is None
                    or activation_expires_at < login_at
                ):
                    return None
                user.state = "active"
                user.activation_nonce = new_id()
                await self._complete_organization_activation(
                    session,
                    user.organization_id,
                    login_at,
                )
                session.add(
                    ControlPlaneAuditEvent(
                        organization_id=user.organization_id,
                        actor_type="organization_user",
                        actor_id=user.id,
                        event_type="administrator.google_activated",
                        details_json="{}",
                        created_at=login_at,
                    )
                )
            user.last_login_at = login_at
            await session.commit()
            return UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )

    async def authorize_microsoft_user(
        self,
        designator: str,
        email: str,
        *,
        issuer: str,
        subject: str,
        activation_nonce: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[UserRecord]:
        """Authorize a bound Entra identity or bind it through an invitation."""
        clean_designator = normalize_designator(designator)
        clean_email = normalize_email(email)
        clean_issuer = issuer.strip()
        clean_subject = subject.strip()
        if not clean_issuer or not clean_subject:
            return None
        login_at = now or utc_now()
        async with self.sessions() as session:
            user = await session.scalar(
                select(OrganizationUser)
                .join(
                    Organization,
                    Organization.id == OrganizationUser.organization_id,
                )
                .join(
                    OrganizationExternalIdentity,
                    OrganizationExternalIdentity.user_id == OrganizationUser.id,
                )
                .where(
                    Organization.designator == clean_designator,
                    OrganizationExternalIdentity.provider == "microsoft",
                    OrganizationExternalIdentity.issuer == clean_issuer,
                    OrganizationExternalIdentity.subject == clean_subject,
                    OrganizationUser.state.in_(("active", "invited")),
                )
            )
            if user is None:
                if not activation_nonce:
                    return None
                user = await session.scalar(
                    select(OrganizationUser)
                    .join(
                        Organization,
                        Organization.id == OrganizationUser.organization_id,
                    )
                    .where(
                        Organization.designator == clean_designator,
                        OrganizationUser.email == clean_email,
                        OrganizationUser.state == "invited",
                    )
                )
                if user is None or not secrets.compare_digest(
                    user.activation_nonce, activation_nonce
                ):
                    return None
                session.add(
                    OrganizationExternalIdentity(
                        organization_id=user.organization_id,
                        user_id=user.id,
                        provider="microsoft",
                        issuer=clean_issuer,
                        subject=clean_subject,
                        created_at=login_at,
                    )
                )
            if user.state == "invited":
                if not activation_nonce or not secrets.compare_digest(
                    user.activation_nonce, activation_nonce
                ):
                    return None
                activation_expires_at = as_utc(user.activation_expires_at)
                if activation_expires_at is None or activation_expires_at < login_at:
                    return None
                user.state = "active"
                user.activation_nonce = new_id()
                await self._complete_organization_activation(
                    session, user.organization_id, login_at
                )
                session.add(
                    ControlPlaneAuditEvent(
                        organization_id=user.organization_id,
                        actor_type="organization_user",
                        actor_id=user.id,
                        event_type="administrator.microsoft_activated",
                        details_json="{}",
                        created_at=login_at,
                    )
                )
            user.last_login_at = login_at
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            return UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )

    async def activate_owner(
        self,
        designator: str,
        email: str,
        password: str,
        now: Optional[datetime] = None,
        *,
        activation_nonce: Optional[str] = None,
    ) -> UserRecord:
        clean_designator = normalize_designator(designator)
        clean_email = normalize_email(email)
        activated_at = now or utc_now()
        async with self.sessions() as session:
            user = await session.scalar(
                select(OrganizationUser)
                .join(
                    Organization,
                    Organization.id == OrganizationUser.organization_id,
                )
                .where(
                    Organization.designator == clean_designator,
                    OrganizationUser.email == clean_email,
                    OrganizationUser.state == "invited",
                )
            )
            if user is None:
                raise ControlPlaneError("No pending administrator invitation found.")
            if activation_nonce is not None and not secrets.compare_digest(
                user.activation_nonce,
                activation_nonce,
            ):
                raise ControlPlaneError("The administrator invitation is invalid.")
            activation_expires_at = as_utc(user.activation_expires_at)
            if activation_expires_at is None or activation_expires_at < activated_at:
                raise ControlPlaneError("The administrator invitation has expired.")
            user.password_hash = hash_password(password)
            user.state = "active"
            user.activation_nonce = new_id()
            await self._complete_organization_activation(
                session,
                user.organization_id,
                activated_at,
            )
            audit = ControlPlaneAuditEvent(
                organization_id=user.organization_id,
                actor_type="organization_user",
                actor_id=user.id,
                event_type="administrator.activated",
                details_json="{}",
                created_at=activated_at,
            )
            session.add(audit)
            await session.commit()
            return UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )

    async def list_users(self, organization_id: str) -> tuple[UserRecord, ...]:
        async with self.sessions() as session:
            users = (
                await session.scalars(
                    select(OrganizationUser)
                    .where(OrganizationUser.organization_id == organization_id)
                    .order_by(OrganizationUser.display_name)
                )
            ).all()
        return tuple(
            UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )
            for user in users
        )

    async def add_user(
        self,
        *,
        organization_id: str,
        display_name: str,
        email: str,
        roles: tuple[str, ...],
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> UserRecord:
        created_at = now or utc_now()
        clean_name = display_name.strip()
        clean_email = normalize_email(email)
        if not clean_name:
            raise ControlPlaneError("Enter the member's name.")
        user = OrganizationUser(
            id=new_id(),
            organization_id=organization_id,
            display_name=clean_name,
            email=clean_email,
            state="invited",
            activation_expires_at=created_at + timedelta(days=7),
            created_at=created_at,
        )
        user.set_roles(roles)
        async with self.sessions() as session:
            organization = await session.get(Organization, organization_id)
            if organization is None:
                raise ControlPlaneError("Organization not found.")
            existing = await session.scalar(
                select(OrganizationUser.id).where(
                    OrganizationUser.organization_id == organization_id,
                    OrganizationUser.email == clean_email,
                )
            )
            if existing:
                raise ControlPlaneError("That email is already an organization member.")
            session.add(user)
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="organization_user",
                    actor_id=actor_id,
                    event_type="member.invited",
                    details_json=json.dumps(
                        {"user_id": user.id, "roles": list(user.roles)}
                    ),
                    created_at=created_at,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ControlPlaneError(
                    "That email is already an organization member."
                ) from exc
        return UserRecord(
            id=user.id,
            organization_id=user.organization_id,
            email=user.email,
            display_name=user.display_name,
            roles=user.roles,
            state=user.state,
        )

    async def update_user(
        self,
        *,
        organization_id: str,
        user_id: str,
        display_name: str,
        email: str,
        roles: tuple[str, ...],
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> UserRecord:
        updated_at = now or utc_now()
        clean_name = display_name.strip()
        clean_email = normalize_email(email)
        if not clean_name or len(clean_name) > 160:
            raise ControlPlaneError("Enter the member's name.")
        if len(clean_email) > 320:
            raise ControlPlaneError("Enter a valid member email address.")
        async with self.sessions() as session:
            user = await session.get(OrganizationUser, user_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Member not found.")
            if "organization_owner" in user.roles:
                raise ControlPlaneError(
                    "The organization owner must be edited from platform administration."
                )
            duplicate = await session.scalar(
                select(OrganizationUser.id).where(
                    OrganizationUser.organization_id == organization_id,
                    OrganizationUser.email == clean_email,
                    OrganizationUser.id != user_id,
                )
            )
            if duplicate:
                raise ControlPlaneError("That email is already an organization member.")
            previous = {
                "display_name": user.display_name,
                "email": user.email,
                "roles": list(user.roles),
            }
            user.display_name = clean_name
            user.email = clean_email
            user.set_roles(roles)
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="organization_user",
                    actor_id=actor_id,
                    event_type="member.updated",
                    details_json=json.dumps(
                        {
                            "user_id": user.id,
                            "previous": previous,
                            "updated": {
                                "display_name": user.display_name,
                                "email": user.email,
                                "roles": list(user.roles),
                            },
                        }
                    ),
                    created_at=updated_at,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ControlPlaneError(
                    "That email is already an organization member."
                ) from exc
            return UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )

    async def delete_user(
        self,
        *,
        organization_id: str,
        user_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> UserRecord:
        deleted_at = now or utc_now()
        async with self.sessions() as session:
            user = await session.get(OrganizationUser, user_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Member not found.")
            if "organization_owner" in user.roles:
                raise ControlPlaneError(
                    "The organization owner cannot be deleted here. "
                    "Replace the administrator from platform administration."
                )
            if user.state == "disabled":
                raise ControlPlaneError("That member is already deleted.")
            previous_state = user.state
            user.state = "disabled"
            user.password_hash = ""
            user.activation_nonce = new_id()
            user.activation_expires_at = None
            await session.execute(
                update(OrganizationPasswordResetToken)
                .where(
                    OrganizationPasswordResetToken.organization_id
                    == organization_id,
                    OrganizationPasswordResetToken.user_id == user_id,
                    OrganizationPasswordResetToken.consumed_at.is_(None),
                )
                .values(consumed_at=deleted_at)
            )
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="organization_user",
                    actor_id=actor_id,
                    event_type="member.deleted",
                    details_json=json.dumps(
                        {"user_id": user.id, "previous_state": previous_state}
                    ),
                    created_at=deleted_at,
                )
            )
            await session.commit()
            return UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )

    async def restore_user(
        self,
        *,
        organization_id: str,
        user_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> UserRecord:
        restored_at = now or utc_now()
        async with self.sessions() as session:
            user = await session.get(OrganizationUser, user_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Member not found.")
            if "organization_owner" in user.roles:
                raise ControlPlaneError(
                    "The organization owner cannot be restored here. "
                    "Manage ownership from platform administration."
                )
            if user.state not in {"disabled", "archived"}:
                raise ControlPlaneError(
                    "Only a deleted or archived member can be restored."
                )
            previous_state = user.state
            user.state = "invited"
            user.password_hash = ""
            user.activation_nonce = new_id()
            user.activation_expires_at = restored_at + timedelta(days=7)
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="organization_user",
                    actor_id=actor_id,
                    event_type="member.restored",
                    details_json=json.dumps(
                        {"user_id": user.id, "previous_state": previous_state}
                    ),
                    created_at=restored_at,
                )
            )
            await session.commit()
            return UserRecord(
                id=user.id,
                organization_id=user.organization_id,
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                state=user.state,
            )

    async def update_settings(
        self,
        *,
        organization_id: str,
        records_visibility: str,
        record_retention_days: int,
        log_retention_days: int,
        notification_email: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> None:
        if records_visibility not in {"public", "restricted"}:
            raise ControlPlaneError("Invalid flight-record visibility.")
        if not 30 <= record_retention_days <= 3650:
            raise ControlPlaneError("Record retention must be 30-3650 days.")
        if not 1 <= log_retention_days <= 730:
            raise ControlPlaneError("Flight-log retention must be 1-730 days.")
        clean_email = normalize_email(notification_email)
        updated_at = now or utc_now()
        async with self.sessions() as session:
            organization = await session.get(Organization, organization_id)
            if organization is None:
                raise ControlPlaneError("Organization not found.")
            organization.records_visibility = records_visibility
            organization.record_retention_days = record_retention_days
            organization.log_retention_days = log_retention_days
            organization.notification_email = clean_email
            organization.updated_at = updated_at
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="organization_user",
                    actor_id=actor_id,
                    event_type="organization.settings_updated",
                    details_json=json.dumps(
                        {
                            "records_visibility": records_visibility,
                            "record_retention_days": record_retention_days,
                            "log_retention_days": log_retention_days,
                        }
                    ),
                    created_at=updated_at,
                )
            )
            await session.commit()

    async def append_ledger_entry(
        self,
        *,
        organization_id: str,
        entry_type: str,
        amount: Decimal,
        description: str,
        idempotency_key: str,
        created_by_type: str,
        created_by_id: str,
        external_reference: str = "",
        now: Optional[datetime] = None,
    ) -> BillingLedgerRecord:
        allowed_types = {
            "charge",
            "credit",
            "payment",
            "refund",
            "adjustment",
            "expiration",
        }
        if entry_type not in allowed_types:
            raise ControlPlaneError("Invalid billing ledger entry type.")
        clean_description = description.strip()
        clean_key = idempotency_key.strip()
        if not clean_description or not clean_key:
            raise ControlPlaneError(
                "Billing description and idempotency key are required."
            )
        created_at = now or utc_now()
        entry = BillingLedgerEntry(
            id=new_id(),
            organization_id=organization_id,
            entry_type=entry_type,
            amount=Decimal(amount).quantize(Decimal("0.0001")),
            description=clean_description,
            idempotency_key=clean_key,
            external_reference=external_reference.strip(),
            created_by_type=created_by_type,
            created_by_id=created_by_id,
            created_at=created_at,
        )
        async with self.sessions() as session:
            organization = await session.get(Organization, organization_id)
            if organization is None:
                raise ControlPlaneError("Organization not found.")
            existing = await session.scalar(
                select(BillingLedgerEntry).where(
                    BillingLedgerEntry.idempotency_key == clean_key
                )
            )
            if existing is not None:
                if (
                    existing.organization_id != organization_id
                    or existing.entry_type != entry_type
                    or Decimal(existing.amount) != entry.amount
                ):
                    raise ControlPlaneError(
                        "Billing idempotency key conflicts with an existing entry."
                    )
                return self._ledger_record(existing)
            session.add(entry)
            await session.flush()
            await self._reconcile_organization_funding_state(
                session,
                organization,
                created_at,
            )
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type=created_by_type,
                    actor_id=created_by_id,
                    event_type="billing.ledger_entry_appended",
                    details_json=json.dumps(
                        {
                            "entry_id": entry.id,
                            "entry_type": entry_type,
                            "amount": str(entry.amount),
                        }
                    ),
                    created_at=created_at,
                )
            )
            await session.commit()
        return self._ledger_record(entry)

    async def _organization_funding_totals(
        self,
        session: AsyncSession,
        organization_id: str,
    ) -> tuple[Decimal, Decimal]:
        ledger_amounts = await session.scalars(
            select(BillingLedgerEntry.amount).where(
                BillingLedgerEntry.organization_id == organization_id
            )
        )
        deposited = sum(
            (Decimal(amount) for amount in ledger_amounts),
            Decimal("0"),
        )
        usage_rows = (
            await session.execute(
                select(
                    UsageDaily.compute_cost,
                    UsageDaily.network_cost,
                    UsageDaily.storage_cost,
                    UsageDaily.database_cost,
                    UsageDaily.faa_proxy_cost,
                    UsageDaily.turn_relay_cost,
                    UsageDaily.other_cost,
                ).where(UsageDaily.organization_id == organization_id)
            )
        ).all()
        usage_cost = sum(
            (
                sum((Decimal(cost) for cost in row), Decimal("0"))
                for row in usage_rows
            ),
            Decimal("0"),
        )
        return deposited, usage_cost

    async def _reconcile_organization_funding_state(
        self,
        session: AsyncSession,
        organization: Organization,
        changed_at: datetime,
    ) -> None:
        if organization.lifecycle_state == "archived":
            return
        subscription = await session.scalar(
            select(Subscription).where(
                Subscription.organization_id == organization.id
            )
        )
        organization.lifecycle_state = "extended_beta"
        organization.billing_mode = "extended beta"
        organization.trial_starts_at = None
        organization.trial_ends_at = None
        if subscription is not None:
            subscription.state = "extended_beta"
            subscription.collection_method = "none"
            subscription.billing_cadence = "calendar month allowance"
            subscription.trial_starts_at = None
            subscription.trial_ends_at = None
            subscription.updated_at = changed_at
        organization.updated_at = changed_at

    async def queue_lifecycle_deadline_notifications(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        checked_at = now or utc_now()
        lifecycle_types = (
            "trial_ending_7d",
            "trial_ending_1d",
            "trial_ended",
            "grace_ending_7d",
            "grace_ending_1d",
            "grace_ended",
        )
        async with self.sessions() as session:
            pending_rows = (
                await session.execute(
                    select(BillingNotification, Organization)
                    .join(
                        Organization,
                        Organization.id == BillingNotification.organization_id,
                    )
                    .where(
                        BillingNotification.state == "pending",
                        BillingNotification.notification_type.in_(lifecycle_types),
                    )
                )
            ).all()
            for notification, organization in pending_rows:
                expected_prefix = f"{organization.lifecycle_state}_"
                if (
                    not notification.notification_type.startswith(expected_prefix)
                    or organization.trial_ends_at is None
                    or as_utc(notification.deadline_at)
                    != as_utc(organization.trial_ends_at)
                ):
                    notification.state = "canceled"

            organizations = (
                await session.scalars(
                    select(Organization).where(
                        Organization.lifecycle_state.in_(("trial", "grace")),
                        Organization.trial_ends_at.is_not(None),
                    )
                )
            ).all()
            for organization in organizations:
                deadline = as_utc(organization.trial_ends_at)
                remaining = deadline - checked_at
                if remaining > timedelta(days=7):
                    continue
                if remaining > timedelta(days=1):
                    phase = "ending_7d"
                elif remaining > timedelta(0):
                    phase = "ending_1d"
                else:
                    phase = "ended"
                notification_type = f"{organization.lifecycle_state}_{phase}"
                for pending_notification, pending_organization in pending_rows:
                    if (
                        pending_organization.id == organization.id
                        and as_utc(pending_notification.deadline_at) == deadline
                        and pending_notification.notification_type
                        != notification_type
                    ):
                        pending_notification.state = "canceled"
                event_key = (
                    f"lifecycle:{organization.id}:{organization.lifecycle_state}:"
                    f"{deadline.isoformat()}:{phase}"
                )
                values = {
                    "id": new_id(),
                    "organization_id": organization.id,
                    "notification_type": notification_type,
                    "event_key": event_key,
                    "state": "pending",
                    "deadline_at": deadline,
                    "created_at": checked_at,
                }
                insert = (
                    postgresql_insert(BillingNotification)
                    if self.engine.dialect.name == "postgresql"
                    else sqlite_insert(BillingNotification)
                ).values(**values)
                await session.execute(
                    insert.on_conflict_do_nothing(index_elements=["event_key"])
                )
            await session.commit()

    @staticmethod
    def _extended_beta_allowance_record(
        allowance: ExtendedBetaAllowance,
    ) -> ExtendedBetaAllowanceRecord:
        return ExtendedBetaAllowanceRecord(
            organization_id=allowance.organization_id,
            billing_month=allowance.billing_month,
            allowance_amount=Decimal(allowance.allowance_amount),
            actual_cost=Decimal(allowance.actual_cost),
            forecast_cost=Decimal(allowance.forecast_cost),
            billing_data_through=as_utc(allowance.billing_data_through),
            video_disabled_at=as_utc(allowance.video_disabled_at),
            month_ends_at=as_utc(allowance.month_ends_at),
        )

    async def reconcile_extended_beta_allowances(
        self,
        *,
        billing_month: str,
        billing_data_through: datetime,
        actual_costs: dict[str, Decimal],
        forecast_costs: dict[str, Decimal],
        now: Optional[datetime] = None,
    ) -> tuple[ExtendedBetaAllowanceRecord, ...]:
        reconciled_at = now or utc_now()
        expected_month = reconciled_at.strftime("%Y-%m")
        if billing_month != expected_month:
            raise ControlPlaneError("Allowance data must be for the current month.")
        last_day = monthrange(reconciled_at.year, reconciled_at.month)[1]
        month_ends_at = datetime(
            reconciled_at.year,
            reconciled_at.month,
            last_day,
            23,
            59,
            59,
            tzinfo=UTC,
        )
        changed_organizations = set()
        async with self.sessions() as session:
            organizations = (
                await session.scalars(
                    select(Organization).where(
                        Organization.lifecycle_state != "archived"
                    )
                )
            ).all()
            results = []
            for organization in organizations:
                actual = max(Decimal("0"), Decimal(actual_costs.get(
                    organization.id, Decimal("0")
                )))
                forecast = max(actual, Decimal(forecast_costs.get(
                    organization.id, actual
                )))
                allowance = await session.scalar(
                    select(ExtendedBetaAllowance).where(
                        ExtendedBetaAllowance.organization_id == organization.id,
                        ExtendedBetaAllowance.billing_month == billing_month,
                    ).with_for_update()
                )
                if allowance is None:
                    values = {
                        "id": new_id(),
                        "organization_id": organization.id,
                        "billing_month": billing_month,
                        "allowance_amount": EXTENDED_BETA_MONTHLY_ALLOWANCE,
                        "actual_cost": actual,
                        "forecast_cost": forecast,
                        "billing_data_through": billing_data_through,
                        "month_ends_at": month_ends_at,
                        "created_at": reconciled_at,
                        "updated_at": reconciled_at,
                    }
                    insert = (
                        postgresql_insert(ExtendedBetaAllowance)
                        if self.engine.dialect.name == "postgresql"
                        else sqlite_insert(ExtendedBetaAllowance)
                    ).values(**values)
                    await session.execute(insert.on_conflict_do_nothing(
                        index_elements=["organization_id", "billing_month"]
                    ))
                    allowance = await session.scalar(
                        select(ExtendedBetaAllowance).where(
                            ExtendedBetaAllowance.organization_id == organization.id,
                            ExtendedBetaAllowance.billing_month == billing_month,
                        ).with_for_update()
                    )
                allowance.actual_cost = actual
                allowance.forecast_cost = forecast
                allowance.billing_data_through = billing_data_through
                allowance.month_ends_at = month_ends_at
                allowance.updated_at = reconciled_at

                notification_types = []
                if forecast > EXTENDED_BETA_MONTHLY_ALLOWANCE:
                    notification_types.append("beta_allowance_on_track")
                if actual > EXTENDED_BETA_MONTHLY_ALLOWANCE:
                    notification_types.append("beta_allowance_exceeded")
                if (
                    actual >= EXTENDED_BETA_VIDEO_CUTOFF
                    and allowance.video_disabled_at is None
                ):
                    allowance.video_disabled_at = reconciled_at
                    notification_types.append("beta_video_disabled")
                    changed_organizations.add(organization.id)
                    active_requests = (
                        await session.scalars(
                            select(VideoStreamRequest).where(
                                VideoStreamRequest.organization_id == organization.id,
                                VideoStreamRequest.state.in_((
                                    "pending", "probing", "awaiting_approval",
                                    "approved", "streaming",
                                )),
                            )
                        )
                    ).all()
                    request_ids = [request.id for request in active_requests]
                    for request in active_requests:
                        request.state = "stopped"
                        request.status_message = (
                            "Remote video disabled for the remainder of the "
                            "extended beta allowance month."
                        )
                        request.stopped_at = reconciled_at
                    if request_ids:
                        await session.execute(delete(VideoPreflightExchange).where(
                            VideoPreflightExchange.request_id.in_(request_ids)
                        ))
                        await session.execute(delete(VideoMediaExchange).where(
                            VideoMediaExchange.request_id.in_(request_ids)
                        ))
                    session.add(ControlPlaneAuditEvent(
                        organization_id=organization.id,
                        actor_type="billing_system",
                        actor_id="extended-beta-allowance",
                        event_type="billing.video_disabled",
                        details_json=json.dumps({
                            "billing_month": billing_month,
                            "actual_cost": str(actual),
                            "allowance": str(EXTENDED_BETA_MONTHLY_ALLOWANCE),
                            "month_ends_at": month_ends_at.isoformat(),
                        }),
                        created_at=reconciled_at,
                    ))
                for notification_type in notification_types:
                    values = {
                        "id": new_id(),
                        "organization_id": organization.id,
                        "notification_type": notification_type,
                        "event_key": (
                            f"extended-beta:{organization.id}:{billing_month}:"
                            f"{notification_type}"
                        ),
                        "state": "pending",
                        "deadline_at": month_ends_at,
                        "created_at": reconciled_at,
                    }
                    insert = (
                        postgresql_insert(BillingNotification)
                        if self.engine.dialect.name == "postgresql"
                        else sqlite_insert(BillingNotification)
                    ).values(**values)
                    await session.execute(
                        insert.on_conflict_do_nothing(index_elements=["event_key"])
                    )
                results.append(allowance)
            for organization_id in changed_organizations:
                await self._notify_video_stream_change(session, organization_id)
            await session.commit()
            return tuple(self._extended_beta_allowance_record(item) for item in results)

    async def get_extended_beta_allowance(
        self,
        organization_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[ExtendedBetaAllowanceRecord]:
        checked_at = now or utc_now()
        async with self.sessions() as session:
            allowance = await session.scalar(
                select(ExtendedBetaAllowance).where(
                    ExtendedBetaAllowance.organization_id == organization_id,
                    ExtendedBetaAllowance.billing_month == checked_at.strftime("%Y-%m"),
                )
            )
        return (
            self._extended_beta_allowance_record(allowance)
            if allowance is not None else None
        )

    async def list_pending_billing_notifications(
        self,
        limit: int = 20,
    ) -> tuple[BillingNotificationRecord, ...]:
        safe_limit = max(1, min(limit, 100))
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        BillingNotification,
                        Organization,
                        OrganizationContact,
                    )
                    .join(
                        Organization,
                        Organization.id == BillingNotification.organization_id,
                    )
                    .join(
                        OrganizationContact,
                        OrganizationContact.organization_id == Organization.id,
                    )
                    .where(
                        BillingNotification.state == "pending",
                        OrganizationContact.contact_role == "primary_admin",
                        OrganizationContact.notifications_enabled.is_(True),
                        Organization.lifecycle_state != "archived",
                    )
                    .order_by(BillingNotification.created_at)
                    .limit(safe_limit)
                )
            ).all()
            organization_ids = {organization.id for _, organization, _ in rows}
            billing_users = (
                await session.scalars(
                    select(OrganizationUser).where(
                        OrganizationUser.organization_id.in_(organization_ids),
                        OrganizationUser.state == "active",
                    ).order_by(OrganizationUser.created_at)
                )
            ).all() if organization_ids else []
        billing_admins = {}
        for user in billing_users:
            if "billing_admin" in user.roles and user.organization_id not in billing_admins:
                billing_admins[user.organization_id] = user
        return tuple(
            BillingNotificationRecord(
                id=notification.id,
                organization_id=organization.id,
                designator=organization.designator,
                organization_name=organization.legal_name,
                administrator_name=(
                    billing_admins[organization.id].display_name
                    if organization.id in billing_admins else contact.name
                ),
                administrator_email=(
                    billing_admins[organization.id].email
                    if organization.id in billing_admins
                    else (organization.notification_email or contact.email)
                ),
                notification_type=notification.notification_type,
                deadline_at=as_utc(notification.deadline_at),
            )
            for notification, organization, contact in rows
        )

    async def mark_billing_notification_sent(
        self,
        notification_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        sent_at = now or utc_now()
        async with self.sessions() as session:
            notification = await session.get(BillingNotification, notification_id)
            if notification is None or notification.state == "sent":
                return
            notification.state = "sent"
            notification.sent_at = sent_at
            notification.attempts += 1
            notification.last_error = ""
            await session.commit()

    async def mark_billing_notification_failed(
        self,
        notification_id: str,
        error: str,
    ) -> None:
        async with self.sessions() as session:
            notification = await session.get(BillingNotification, notification_id)
            if notification is None or notification.state == "sent":
                return
            notification.attempts += 1
            notification.last_error = error.strip()[:240]
            await session.commit()

    async def list_ledger(
        self,
        organization_id: str,
        limit: int = 50,
    ) -> tuple[BillingLedgerRecord, ...]:
        safe_limit = max(1, min(limit, 200))
        async with self.sessions() as session:
            entries = (
                await session.scalars(
                    select(BillingLedgerEntry)
                    .where(BillingLedgerEntry.organization_id == organization_id)
                    .order_by(BillingLedgerEntry.created_at.desc())
                    .limit(safe_limit)
                )
            ).all()
        return tuple(self._ledger_record(entry) for entry in entries)

    async def collected_month_to_date(
        self,
        now: Optional[datetime] = None,
    ) -> Decimal:
        checked_at = now or utc_now()
        month_start = checked_at.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        async with self.sessions() as session:
            entries = (
                await session.scalars(
                    select(BillingLedgerEntry).where(
                        BillingLedgerEntry.entry_type == "payment",
                        BillingLedgerEntry.created_at >= month_start,
                    )
                )
            ).all()
        return sum(
            (Decimal(entry.amount) for entry in entries),
            Decimal("0.00"),
        )

    async def record_daily_usage(
        self,
        *,
        organization_id: str,
        usage_date: str,
        compute_units: Decimal = Decimal("0"),
        compute_cost: Decimal = Decimal("0"),
        network_bytes: int = 0,
        network_cost: Decimal = Decimal("0"),
        storage_byte_days: int = 0,
        storage_cost: Decimal = Decimal("0"),
        database_units: Decimal = Decimal("0"),
        database_cost: Decimal = Decimal("0"),
        faa_proxy_requests: int = 0,
        faa_proxy_cost: Decimal = Decimal("0"),
        turn_relay_bytes: int = 0,
        turn_relay_cost: Decimal = Decimal("0"),
        other_cost: Decimal = Decimal("0"),
        now: Optional[datetime] = None,
    ) -> None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", usage_date):
            raise ControlPlaneError("Usage date must use YYYY-MM-DD.")
        integer_values = (
            network_bytes,
            storage_byte_days,
            faa_proxy_requests,
            turn_relay_bytes,
        )
        decimal_values = (
            compute_units,
            compute_cost,
            network_cost,
            storage_cost,
            database_units,
            database_cost,
            faa_proxy_cost,
            turn_relay_cost,
            other_cost,
        )
        if any(value < 0 for value in integer_values) or any(
            Decimal(value) < 0 for value in decimal_values
        ):
            raise ControlPlaneError("Aggregate usage values cannot be negative.")
        async with self.sessions() as session:
            organization = await session.get(Organization, organization_id)
            if organization is None:
                raise ControlPlaneError("Organization not found.")
            usage = await session.scalar(
                select(UsageDaily).where(
                    UsageDaily.organization_id == organization_id,
                    UsageDaily.usage_date == usage_date,
                )
            )
            if usage is None:
                usage = UsageDaily(
                    id=new_id(),
                    organization_id=organization_id,
                    usage_date=usage_date,
                )
                session.add(usage)
            usage.compute_units = Decimal(compute_units)
            usage.compute_cost = Decimal(compute_cost)
            usage.network_bytes = network_bytes
            usage.network_cost = Decimal(network_cost)
            usage.storage_byte_days = storage_byte_days
            usage.storage_cost = Decimal(storage_cost)
            usage.database_units = Decimal(database_units)
            usage.database_cost = Decimal(database_cost)
            usage.faa_proxy_requests = faa_proxy_requests
            usage.faa_proxy_cost = Decimal(faa_proxy_cost)
            usage.turn_relay_bytes = turn_relay_bytes
            usage.turn_relay_cost = Decimal(turn_relay_cost)
            usage.other_cost = Decimal(other_cost)
            usage.recorded_at = now or utc_now()
            await session.flush()
            await self._reconcile_organization_funding_state(
                session,
                organization,
                usage.recorded_at,
            )
            await session.commit()

    async def increment_daily_usage(
        self,
        *,
        organization_id: str,
        usage_date: Optional[str] = None,
        compute_units: Decimal = Decimal("0"),
        network_bytes: int = 0,
        storage_byte_days: int = 0,
        database_units: Decimal = Decimal("0"),
        faa_proxy_requests: int = 0,
        turn_relay_bytes: int = 0,
        now: Optional[datetime] = None,
    ) -> None:
        """Atomically add privacy-safe counters to one organization's daily row."""
        recorded_at = now or utc_now()
        day = usage_date or recorded_at.strftime("%Y-%m-%d")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ControlPlaneError("Usage date must use YYYY-MM-DD.")
        integer_values = (
            network_bytes,
            storage_byte_days,
            faa_proxy_requests,
            turn_relay_bytes,
        )
        decimal_values = (compute_units, database_units)
        if any(value < 0 for value in integer_values) or any(
            Decimal(value) < 0 for value in decimal_values
        ):
            raise ControlPlaneError("Aggregate usage values cannot be negative.")

        values = {
            "id": new_id(),
            "organization_id": organization_id,
            "usage_date": day,
            "compute_units": Decimal(compute_units),
            "network_bytes": network_bytes,
            "storage_byte_days": storage_byte_days,
            "database_units": Decimal(database_units),
            "faa_proxy_requests": faa_proxy_requests,
            "turn_relay_bytes": turn_relay_bytes,
            "recorded_at": recorded_at,
        }
        async with self.sessions() as session:
            if await session.get(Organization, organization_id) is None:
                raise ControlPlaneError("Organization not found.")
            insert = (
                postgresql_insert(UsageDaily)
                if self.engine.dialect.name == "postgresql"
                else sqlite_insert(UsageDaily)
            ).values(**values)
            excluded = insert.excluded
            insert = insert.on_conflict_do_update(
                index_elements=["organization_id", "usage_date"],
                set_={
                    "compute_units": UsageDaily.compute_units + excluded.compute_units,
                    "network_bytes": UsageDaily.network_bytes + excluded.network_bytes,
                    "storage_byte_days": (
                        UsageDaily.storage_byte_days + excluded.storage_byte_days
                    ),
                    "database_units": (
                        UsageDaily.database_units + excluded.database_units
                    ),
                    "faa_proxy_requests": (
                        UsageDaily.faa_proxy_requests + excluded.faa_proxy_requests
                    ),
                    "turn_relay_bytes": (
                        UsageDaily.turn_relay_bytes + excluded.turn_relay_bytes
                    ),
                    "recorded_at": recorded_at,
                },
            )
            await session.execute(insert)
            await session.commit()

    async def month_to_date_usage_costs(
        self,
        now: Optional[datetime] = None,
    ) -> dict[str, UsageCostRecord]:
        checked_at = now or utc_now()
        month_prefix = checked_at.strftime("%Y-%m-")
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(UsageDaily).where(
                        UsageDaily.usage_date.startswith(month_prefix)
                    )
                )
            ).all()
        totals: dict[str, dict[str, Decimal]] = {}
        for row in rows:
            organization_totals = totals.setdefault(
                row.organization_id,
                {
                    "compute": Decimal("0"),
                    "network": Decimal("0"),
                    "storage": Decimal("0"),
                    "database": Decimal("0"),
                    "other": Decimal("0"),
                },
            )
            organization_totals["compute"] += Decimal(row.compute_cost)
            organization_totals["network"] += Decimal(row.network_cost)
            organization_totals["storage"] += Decimal(row.storage_cost)
            organization_totals["database"] += Decimal(row.database_cost)
            organization_totals["other"] += (
                Decimal(row.faa_proxy_cost)
                + Decimal(row.turn_relay_cost)
                + Decimal(row.other_cost)
            )
        return {
            organization_id: UsageCostRecord(
                organization_id=organization_id,
                **values,
            )
            for organization_id, values in totals.items()
        }

    async def month_to_date_usage_aggregates(
        self,
        now: Optional[datetime] = None,
    ) -> dict[str, UsageAggregateRecord]:
        checked_at = now or utc_now()
        month_prefix = checked_at.strftime("%Y-%m-")
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        UsageDaily.organization_id,
                        func.sum(UsageDaily.compute_units),
                        func.sum(UsageDaily.network_bytes),
                        func.sum(UsageDaily.storage_byte_days),
                        func.sum(UsageDaily.database_units),
                        func.sum(UsageDaily.faa_proxy_requests),
                        func.sum(UsageDaily.turn_relay_bytes),
                    )
                    .where(UsageDaily.usage_date.startswith(month_prefix))
                    .group_by(UsageDaily.organization_id)
                )
            ).all()
        return {
            organization_id: UsageAggregateRecord(
                organization_id=organization_id,
                compute_units=Decimal(compute_units or 0),
                network_bytes=int(network_bytes or 0),
                storage_byte_days=int(storage_byte_days or 0),
                database_units=Decimal(database_units or 0),
                faa_proxy_requests=int(faa_proxy_requests or 0),
                turn_relay_bytes=int(turn_relay_bytes or 0),
            )
            for (
                organization_id,
                compute_units,
                network_bytes,
                storage_byte_days,
                database_units,
                faa_proxy_requests,
                turn_relay_bytes,
            ) in rows
        }

    async def list_provisioning_jobs(
        self,
        limit: int = 50,
    ) -> tuple[ProvisioningJobRecord, ...]:
        safe_limit = max(1, min(limit, 200))
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(ProvisioningJob, Organization.designator)
                    .join(
                        Organization,
                        Organization.id == ProvisioningJob.organization_id,
                    )
                    .order_by(ProvisioningJob.created_at.desc())
                    .limit(safe_limit)
                )
            ).all()
        return tuple(
            ProvisioningJobRecord(
                id=job.id,
                organization_id=job.organization_id,
                designator=designator,
                state=job.state,
                current_step=job.current_step,
                simulation=job.simulation,
                steps=tuple(json.loads(job.steps_json or "[]")),
                created_at=as_utc(job.created_at),
                completed_at=as_utc(job.completed_at),
            )
            for job, designator in rows
        )

    async def list_audit_events(
        self,
        limit: int = 100,
    ) -> tuple[AuditEventRecord, ...]:
        page = await self.search_audit_events(
            page_size=max(1, min(limit, 500))
        )
        return page.events

    async def claim_external_webhook_delivery(
        self,
        *,
        provider: str,
        event_id: str,
        event_type: str,
        resource_type: str,
        resource_id: str,
        now: Optional[datetime] = None,
    ) -> str:
        clean_provider = provider.strip()
        clean_event_id = event_id.strip()
        if not clean_provider or not clean_event_id:
            raise ValueError("Webhook provider and event ID are required.")
        claimed_at = as_utc(now or utc_now())
        stale_before = claimed_at - timedelta(minutes=5)
        async with self.sessions() as session:
            delivery = await session.scalar(select(ExternalWebhookDelivery).where(
                ExternalWebhookDelivery.provider == clean_provider,
                ExternalWebhookDelivery.event_id == clean_event_id,
            ))
            if delivery is None:
                session.add(ExternalWebhookDelivery(
                    provider=clean_provider,
                    event_id=clean_event_id,
                    event_type=event_type.strip(),
                    resource_type=resource_type.strip(),
                    resource_id=resource_id.strip(),
                    state="processing",
                    attempts=1,
                    created_at=claimed_at,
                    updated_at=claimed_at,
                ))
                try:
                    await session.commit()
                    return "claimed"
                except IntegrityError:
                    await session.rollback()
            delivery = await session.scalar(select(ExternalWebhookDelivery).where(
                ExternalWebhookDelivery.provider == clean_provider,
                ExternalWebhookDelivery.event_id == clean_event_id,
            ))
            if delivery is None:
                raise ControlPlaneError("Webhook delivery claim could not be stored.")
            if delivery.state == "sent":
                return "sent"
            result = await session.execute(
                update(ExternalWebhookDelivery)
                .where(
                    ExternalWebhookDelivery.id == delivery.id,
                    ExternalWebhookDelivery.state != "sent",
                    or_(
                        ExternalWebhookDelivery.state != "processing",
                        ExternalWebhookDelivery.updated_at < stale_before,
                    ),
                )
                .values(
                    state="processing",
                    attempts=ExternalWebhookDelivery.attempts + 1,
                    last_error="",
                    updated_at=claimed_at,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return "claimed" if int(result.rowcount or 0) else "processing"

    async def mark_external_webhook_delivery_sent(
        self,
        *,
        provider: str,
        event_id: str,
        now: Optional[datetime] = None,
    ) -> None:
        sent_at = as_utc(now or utc_now())
        async with self.sessions() as session:
            await session.execute(
                update(ExternalWebhookDelivery)
                .where(
                    ExternalWebhookDelivery.provider == provider.strip(),
                    ExternalWebhookDelivery.event_id == event_id.strip(),
                )
                .values(
                    state="sent",
                    last_error="",
                    updated_at=sent_at,
                    sent_at=sent_at,
                )
            )
            await session.commit()

    async def mark_external_webhook_delivery_failed(
        self,
        *,
        provider: str,
        event_id: str,
        error: str,
        now: Optional[datetime] = None,
    ) -> None:
        failed_at = as_utc(now or utc_now())
        async with self.sessions() as session:
            await session.execute(
                update(ExternalWebhookDelivery)
                .where(
                    ExternalWebhookDelivery.provider == provider.strip(),
                    ExternalWebhookDelivery.event_id == event_id.strip(),
                )
                .values(
                    state="failed",
                    last_error=error.strip()[:500],
                    updated_at=failed_at,
                )
            )
            await session.commit()

    async def search_audit_events(
        self,
        *,
        page: int = 1,
        page_size: int = AUDIT_EVENT_PAGE_SIZE,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        organization_designator: str = "",
        actor_type: str = "",
        event_type: str = "",
        categories: Iterable[str] = (),
    ) -> AuditEventPage:
        safe_page = max(1, page)
        safe_page_size = max(1, min(page_size, AUDIT_EVENT_EXPORT_LIMIT))
        conditions = []
        if start_at is not None:
            conditions.append(ControlPlaneAuditEvent.created_at >= start_at)
        if end_at is not None:
            conditions.append(ControlPlaneAuditEvent.created_at < end_at)
        normalized_designator = organization_designator.strip().upper()
        if normalized_designator:
            conditions.append(Organization.designator == normalized_designator)
        normalized_actor_type = actor_type.strip().lower()
        if normalized_actor_type:
            conditions.append(
                ControlPlaneAuditEvent.actor_type == normalized_actor_type
            )
        normalized_event_type = event_type.strip().lower()
        if normalized_event_type:
            conditions.append(
                ControlPlaneAuditEvent.event_type == normalized_event_type
            )
        category_conditions = []
        for category in categories:
            for prefix in AUDIT_EVENT_CATEGORY_PREFIXES.get(category, ()):
                category_conditions.append(
                    ControlPlaneAuditEvent.event_type.like(f"{prefix}%")
                )
        if category_conditions:
            conditions.append(or_(*category_conditions))

        joined = (
            select(ControlPlaneAuditEvent, Organization.designator)
            .outerjoin(
                Organization,
                Organization.id == ControlPlaneAuditEvent.organization_id,
            )
            .where(*conditions)
        )
        async with self.sessions() as session:
            total = int((await session.execute(
                select(func.count())
                .select_from(ControlPlaneAuditEvent)
                .outerjoin(
                    Organization,
                    Organization.id == ControlPlaneAuditEvent.organization_id,
                )
                .where(*conditions)
            )).scalar_one())
            rows = (
                await session.execute(
                    joined
                    .order_by(ControlPlaneAuditEvent.created_at.desc())
                    .offset((safe_page - 1) * safe_page_size)
                    .limit(safe_page_size)
                )
            ).all()
        events = tuple(
            AuditEventRecord(
                id=event.id,
                organization_id=event.organization_id,
                designator=designator,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                event_type=event.event_type,
                details=json.loads(event.details_json or "{}"),
                created_at=as_utc(event.created_at),
                retention_hold=bool(event.retention_hold),
            )
            for event, designator in rows
        )
        return AuditEventPage(
            events=events,
            total=total,
            page=safe_page,
            page_size=safe_page_size,
        )

    async def purge_expired_audit_events(
        self,
        *,
        now: Optional[datetime] = None,
        retention_days: int = AUDIT_EVENT_RETENTION_DAYS,
    ) -> int:
        reference = as_utc(now or utc_now())
        cutoff = reference - timedelta(days=max(1, retention_days))
        async with self.sessions() as session:
            result = await session.execute(
                delete(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.created_at < cutoff,
                    ControlPlaneAuditEvent.retention_hold.is_(False),
                )
            )
            await session.commit()
            return max(0, int(result.rowcount or 0))

    async def record_audit_access(
        self,
        *,
        actor_id: str,
        event_type: str = "audit.viewed",
        details: Optional[dict] = None,
        now: Optional[datetime] = None,
    ) -> None:
        if event_type not in {"audit.viewed", "audit.exported"}:
            raise ValueError("Unsupported audit access event type.")
        async with self.sessions() as session:
            session.add(ControlPlaneAuditEvent(
                organization_id=None,
                actor_type="platform_admin",
                actor_id=actor_id,
                event_type=event_type,
                details_json=json.dumps(details or {}, sort_keys=True),
                created_at=as_utc(now or utc_now()),
            ))
            await session.commit()

    async def set_audit_event_retention_hold(
        self,
        *,
        event_id: str,
        retention_hold: bool,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> AuditEventRecord:
        changed_at = as_utc(now or utc_now())
        async with self.sessions() as session:
            event = await session.get(ControlPlaneAuditEvent, event_id)
            if event is None:
                raise ControlPlaneError("Audit event was not found.")
            event.retention_hold = retention_hold
            session.add(ControlPlaneAuditEvent(
                organization_id=event.organization_id,
                actor_type="platform_admin",
                actor_id=actor_id,
                event_type=(
                    "audit.retention_hold_placed"
                    if retention_hold
                    else "audit.retention_hold_released"
                ),
                details_json=json.dumps({
                    "target_event_id": event.id,
                    "target_event_type": event.event_type,
                }, sort_keys=True),
                created_at=changed_at,
            ))
            await session.commit()
            designator = None
            if event.organization_id:
                designator = await session.scalar(
                    select(Organization.designator).where(
                        Organization.id == event.organization_id
                    )
                )
            return AuditEventRecord(
                id=event.id,
                organization_id=event.organization_id,
                designator=designator,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                event_type=event.event_type,
                details=json.loads(event.details_json or "{}"),
                created_at=as_utc(event.created_at),
                retention_hold=bool(event.retention_hold),
            )

    async def create_enrollment_campaign(
        self,
        *,
        organization_id: str,
        label: str,
        created_by_user_id: str,
        expires_in_hours: int,
        max_redemptions: int,
        now: Optional[datetime] = None,
    ) -> EnrollmentCampaignRecord:
        created_at = now or utc_now()
        clean_label = label.strip()
        if not clean_label:
            raise ControlPlaneError("Enter an enrollment label.")
        if not 1 <= expires_in_hours <= 24 * 30:
            raise ControlPlaneError("Enrollment validity must be 1-720 hours.")
        if not 1 <= max_redemptions <= 500:
            raise ControlPlaneError("Enrollment uses must be 1-500.")
        campaign = EnrollmentCampaign(
            id=new_id(),
            organization_id=organization_id,
            label=clean_label,
            created_by_user_id=created_by_user_id,
            max_redemptions=max_redemptions,
            token_generation=new_id(),
            expires_at=created_at + timedelta(hours=expires_in_hours),
            created_at=created_at,
        )
        async with self.sessions() as session:
            user = await session.get(OrganizationUser, created_by_user_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Enrollment campaign owner is invalid.")
            session.add(campaign)
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="organization_user",
                    actor_id=created_by_user_id,
                    event_type="enrollment.created",
                    details_json=json.dumps(
                        {
                            "campaign_id": campaign.id,
                            "max_redemptions": max_redemptions,
                            "expires_in_hours": expires_in_hours,
                        }
                    ),
                    created_at=created_at,
                )
            )
            await session.commit()
        return self._campaign_record(campaign)

    async def list_enrollment_campaigns(
        self,
        organization_id: str,
    ) -> tuple[EnrollmentCampaignRecord, ...]:
        checked_at = utc_now()
        async with self.sessions() as session:
            campaigns = (
                await session.scalars(
                    select(EnrollmentCampaign)
                    .where(EnrollmentCampaign.organization_id == organization_id)
                    .order_by(EnrollmentCampaign.created_at.desc())
                )
            ).all()
        return tuple(
            self._campaign_record(campaign, checked_at=checked_at)
            for campaign in campaigns
        )

    async def get_enrollment_campaign(
        self,
        campaign_id: str,
    ) -> Optional[EnrollmentCampaignRecord]:
        async with self.sessions() as session:
            campaign = await session.get(EnrollmentCampaign, campaign_id)
        return None if campaign is None else self._campaign_record(campaign)

    async def revoke_enrollment_campaign(
        self,
        *,
        campaign_id: str,
        organization_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> None:
        revoked_at = now or utc_now()
        async with self.sessions() as session:
            campaign = await session.get(EnrollmentCampaign, campaign_id)
            if campaign is None or campaign.organization_id != organization_id:
                raise ControlPlaneError("Enrollment campaign not found.")
            user = await session.get(OrganizationUser, actor_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Enrollment campaign administrator is invalid.")
            campaign.state = "revoked"
            campaign.revoked_at = revoked_at
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="organization_user",
                    actor_id=actor_id,
                    event_type="enrollment.revoked",
                    details_json=json.dumps({"campaign_id": campaign_id}),
                    created_at=revoked_at,
                )
            )
            await session.commit()

    async def renew_enrollment_campaign(
        self,
        *,
        campaign_id: str,
        organization_id: str,
        actor_id: str,
        expires_in_hours: int = 7 * 24,
        now: Optional[datetime] = None,
    ) -> EnrollmentCampaignRecord:
        renewed_at = as_utc(now or utc_now())
        if not 1 <= expires_in_hours <= 24 * 30:
            raise ControlPlaneError("Enrollment validity must be 1-720 hours.")
        async with self.sessions() as session:
            campaign = await session.get(EnrollmentCampaign, campaign_id)
            if campaign is None or campaign.organization_id != organization_id:
                raise ControlPlaneError("Enrollment campaign not found.")
            user = await session.get(OrganizationUser, actor_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Enrollment campaign administrator is invalid.")
            if campaign.state == "revoked":
                raise ControlPlaneError("A revoked enrollment QR cannot be renewed.")
            if campaign.redemption_count >= campaign.max_redemptions:
                raise ControlPlaneError("Enrollment campaign has no uses remaining.")
            previous_expiry = as_utc(campaign.expires_at)
            campaign.state = "active"
            campaign.expires_at = renewed_at + timedelta(hours=expires_in_hours)
            campaign.revoked_at = None
            campaign.token_generation = new_id()
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id,
                actor_type="organization_user",
                actor_id=actor_id,
                event_type="enrollment.renewed",
                details_json=json.dumps({
                    "campaign_id": campaign.id,
                    "previous_expires_at": previous_expiry.isoformat(),
                    "expires_in_hours": expires_in_hours,
                }, sort_keys=True),
                created_at=renewed_at,
            ))
            await session.commit()
            return self._campaign_record(campaign)

    async def redeem_enrollment_campaign(
        self,
        *,
        campaign_id: str,
        organization_id: str,
        now: Optional[datetime] = None,
    ) -> EnrollmentCampaignRecord:
        redeemed_at = now or utc_now()
        async with self.sessions() as session:
            campaign = await session.get(EnrollmentCampaign, campaign_id)
            if campaign is None or campaign.organization_id != organization_id:
                raise ControlPlaneError("Enrollment campaign not found.")
            expires_at = as_utc(campaign.expires_at)
            if campaign.state != "active" or expires_at is None:
                raise ControlPlaneError("Enrollment campaign is not active.")
            if expires_at < redeemed_at:
                campaign.state = "expired"
                await session.commit()
                raise ControlPlaneError("Enrollment campaign has expired.")
            if campaign.redemption_count >= campaign.max_redemptions:
                campaign.state = "exhausted"
                await session.commit()
                raise ControlPlaneError("Enrollment campaign has no uses remaining.")
            campaign.redemption_count += 1
            if campaign.redemption_count >= campaign.max_redemptions:
                campaign.state = "exhausted"
            await session.commit()
            return self._campaign_record(campaign)

    async def issue_device_credential(
        self,
        *,
        campaign_id: str,
        organization_id: str,
        device_name: str,
        platform: str,
        functionality_release: int = 0,
        authorized_user_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> IssuedDeviceCredential:
        issued_at = now or utc_now()
        clean_name = device_name.strip()
        clean_platform = platform.strip().lower()
        if not clean_name or len(clean_name) > 160:
            raise ControlPlaneError("Enter a device name.")
        if clean_platform not in {"android", "ios"}:
            raise ControlPlaneError("Device platform must be Android or iOS.")
        if functionality_release < 0:
            raise ControlPlaneError("Functionality release must not be negative.")
        token = "r2c_dev_" + secrets.token_urlsafe(32)
        expires_at = issued_at + timedelta(days=365)
        async with self.sessions() as session:
            campaign = await session.scalar(
                select(EnrollmentCampaign)
                .where(EnrollmentCampaign.id == campaign_id)
                .with_for_update()
            )
            if campaign is None or campaign.organization_id != organization_id:
                raise ControlPlaneError("Enrollment campaign not found.")
            campaign_expires_at = as_utc(campaign.expires_at)
            if campaign.state != "active" or campaign_expires_at is None:
                raise ControlPlaneError("Enrollment campaign is not active.")
            if campaign_expires_at < issued_at:
                campaign.state = "expired"
                await session.commit()
                raise ControlPlaneError("Enrollment campaign has expired.")
            if campaign.redemption_count >= campaign.max_redemptions:
                campaign.state = "exhausted"
                await session.commit()
                raise ControlPlaneError("Enrollment campaign has no uses remaining.")
            organization = await session.get(Organization, organization_id)
            if organization is None:
                raise ControlPlaneError("Organization not found.")
            if authorized_user_id is not None:
                authorized_user = await session.get(
                    OrganizationUser, authorized_user_id
                )
                if (
                    authorized_user is None
                    or authorized_user.organization_id != organization_id
                    or authorized_user.state != "active"
                ):
                    raise ControlPlaneError("Authorized R2C user is invalid.")
            credential = DeviceCredential(
                id=new_id(),
                organization_id=organization_id,
                campaign_id=campaign_id,
                device_name=clean_name,
                platform=clean_platform,
                authorized_user_id=authorized_user_id,
                functionality_release=functionality_release,
                token_prefix=token[:16],
                token_hash=device_token_hash(token),
                state="active",
                created_at=issued_at,
                expires_at=expires_at,
            )
            campaign.redemption_count += 1
            if campaign.redemption_count >= campaign.max_redemptions:
                campaign.state = "exhausted"
            session.add(credential)
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="device_enrollment",
                    actor_id=credential.id,
                    event_type="device.credential_issued",
                    details_json=json.dumps(
                        {
                            "campaign_id": campaign_id,
                            "platform": clean_platform,
                            "device_name": clean_name,
                            "authorized_user_id": authorized_user_id,
                            "functionality_release": functionality_release,
                        }
                    ),
                    created_at=issued_at,
                )
            )
            await session.commit()
            return IssuedDeviceCredential(
                id=credential.id,
                organization_id=organization_id,
                designator=organization.designator,
                token=token,
                device_name=clean_name,
                platform=clean_platform,
                expires_at=expires_at,
            )

    async def authenticate_device_token(
        self,
        token: str,
        now: Optional[datetime] = None,
    ) -> Optional[DeviceCredentialRecord]:
        if not token.startswith("r2c_dev_") or len(token) < 40:
            return None
        checked_at = now or utc_now()
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(DeviceCredential, Organization.designator)
                    .join(
                        Organization,
                        Organization.id == DeviceCredential.organization_id,
                    )
                    .where(
                        DeviceCredential.token_hash == device_token_hash(token),
                        DeviceCredential.state == "active",
                    )
                )
            ).first()
            if row is None:
                return None
            credential, designator = row
            expires_at = as_utc(credential.expires_at)
            if expires_at is None or expires_at < checked_at:
                credential.state = "expired"
                await session.commit()
                return None
            credential.last_used_at = checked_at
            await session.commit()
            return DeviceCredentialRecord(
                id=credential.id,
                organization_id=credential.organization_id,
                designator=designator,
                device_name=credential.device_name,
                platform=credential.platform,
                functionality_release=credential.functionality_release,
                expires_at=expires_at,
            )

    async def device_token_state(self, token: str) -> Optional[str]:
        """Return a recognized credential state without authenticating it."""
        if not token.startswith("r2c_dev_") or len(token) < 40:
            return None
        async with self.sessions() as session:
            return await session.scalar(
                select(DeviceCredential.state).where(
                    DeviceCredential.token_hash == device_token_hash(token)
                )
            )

    async def device_reauthentication_challenge(
        self, token: str
    ) -> Optional[tuple[DeviceCredentialAdminRecord, str]]:
        if not token.startswith("r2c_dev_") or len(token) < 40:
            return None
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(DeviceCredential, Organization.designator)
                    .join(
                        Organization,
                        Organization.id == DeviceCredential.organization_id,
                    )
                    .where(
                        DeviceCredential.token_hash == device_token_hash(token),
                        DeviceCredential.state == "reauth_required",
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            credential, designator = row
            return (
                self._device_credential_admin_record(credential, utc_now()),
                designator,
            )

    async def require_device_reauthentication(
        self,
        *,
        credential_id: str,
        organization_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> DeviceCredentialAdminRecord:
        requested_at = as_utc(now or utc_now())
        async with self.sessions() as session:
            credential = await session.get(DeviceCredential, credential_id)
            if credential is None or credential.organization_id != organization_id:
                raise ControlPlaneError("Device credential not found.")
            user = await session.get(OrganizationUser, actor_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Device credential administrator is invalid.")
            credential.state = "reauth_required"
            credential.reauth_requested_at = requested_at
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id,
                actor_type="organization_user",
                actor_id=actor_id,
                event_type="device.reauthentication_required",
                details_json=json.dumps({
                    "credential_id": credential.id,
                    "device_name": credential.device_name,
                    "message": (
                        f"Administrator required {credential.device_name} "
                        "to reauthenticate before restoring access."
                    ),
                }, sort_keys=True),
                created_at=requested_at,
            ))
            await session.commit()
            return self._device_credential_admin_record(
                credential, requested_at
            )

    async def get_device_reauthentication_record(
        self,
        *,
        credential_id: str,
        organization_id: str,
    ) -> Optional[DeviceCredentialAdminRecord]:
        async with self.sessions() as session:
            credential = await session.get(DeviceCredential, credential_id)
            if (
                credential is None
                or credential.organization_id != organization_id
                or credential.state != "reauth_required"
            ):
                return None
            return self._device_credential_admin_record(credential, utc_now())

    async def complete_device_reauthentication(
        self,
        *,
        credential_id: str,
        organization_id: str,
        user_id: str,
        now: Optional[datetime] = None,
    ) -> DeviceCredentialAdminRecord:
        completed_at = as_utc(now or utc_now())
        async with self.sessions() as session:
            credential = await session.get(DeviceCredential, credential_id)
            user = await session.get(OrganizationUser, user_id)
            if (
                credential is None
                or credential.organization_id != organization_id
                or credential.state != "reauth_required"
            ):
                raise ControlPlaneError("Device is not awaiting reauthentication.")
            if (
                user is None
                or user.organization_id != organization_id
                or user.state != "active"
                or "r2c_device" not in user.roles
            ):
                raise ControlPlaneError(
                    "This user is not authorized to operate RID2Caltopo devices."
                )
            credential.state = "active"
            credential.authorized_user_id = user.id
            credential.reauth_requested_at = None
            credential.last_used_at = completed_at
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id,
                actor_type="organization_user",
                actor_id=user.id,
                event_type="device.reauthentication_completed",
                details_json=json.dumps({
                    "credential_id": credential.id,
                    "device_name": credential.device_name,
                    "user_email": user.email,
                    "message": (
                        f"Authorized user {user.email} reauthenticated "
                        f"{credential.device_name}."
                    ),
                }, sort_keys=True),
                created_at=completed_at,
            ))
            await session.commit()
            return self._device_credential_admin_record(credential, completed_at)

    async def list_device_credentials(
        self,
        organization_id: str,
        now: Optional[datetime] = None,
    ) -> tuple[DeviceCredentialAdminRecord, ...]:
        checked_at = as_utc(now or utc_now())
        async with self.sessions() as session:
            credentials = (
                await session.scalars(
                    select(DeviceCredential)
                    .where(DeviceCredential.organization_id == organization_id)
                    .order_by(
                        DeviceCredential.device_name.asc(),
                        DeviceCredential.created_at.desc(),
                    )
                )
            ).all()
        return tuple(
            self._device_credential_admin_record(credential, checked_at)
            for credential in credentials
        )

    async def extend_device_credential(
        self,
        *,
        credential_id: str,
        organization_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> DeviceCredentialAdminRecord:
        extended_at = as_utc(now or utc_now())
        async with self.sessions() as session:
            credential = await session.get(DeviceCredential, credential_id)
            if credential is None or credential.organization_id != organization_id:
                raise ControlPlaneError("Device credential not found.")
            if credential.state == "revoked":
                raise ControlPlaneError("A revoked device credential cannot be extended.")
            if credential.state == "reauth_required":
                raise ControlPlaneError(
                    "This device must complete reauthentication before its credential can be extended."
                )
            user = await session.get(OrganizationUser, actor_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Device credential administrator is invalid.")
            previous_state = credential.state
            previous_expiry = as_utc(credential.expires_at)
            credential.state = "active"
            credential.expires_at = max(
                previous_expiry,
                extended_at + timedelta(days=365),
            )
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id,
                actor_type="organization_user",
                actor_id=actor_id,
                event_type="device.credential_extended",
                details_json=json.dumps({
                    "credential_id": credential.id,
                    "device_name": credential.device_name,
                    "previous_state": previous_state,
                    "previous_expires_at": previous_expiry.isoformat(),
                    "expires_at": as_utc(credential.expires_at).isoformat(),
                }, sort_keys=True),
                created_at=extended_at,
            ))
            await session.commit()
            return self._device_credential_admin_record(credential, extended_at)

    async def extend_all_device_credentials(
        self,
        *,
        organization_id: str,
        actor_id: str,
        now: Optional[datetime] = None,
    ) -> tuple[DeviceCredentialAdminRecord, ...]:
        extended_at = as_utc(now or utc_now())
        async with self.sessions() as session:
            user = await session.get(OrganizationUser, actor_id)
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Device credential administrator is invalid.")
            credentials = (
                await session.scalars(
                    select(DeviceCredential)
                    .where(
                        DeviceCredential.organization_id == organization_id,
                        DeviceCredential.state.not_in(("revoked", "reauth_required")),
                    )
                    .with_for_update()
                )
            ).all()
            if not credentials:
                raise ControlPlaneError("No renewable device credentials were found.")
            minimum_expiry = extended_at + timedelta(days=365)
            for credential in credentials:
                credential.state = "active"
                credential.expires_at = max(
                    as_utc(credential.expires_at),
                    minimum_expiry,
                )
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id,
                actor_type="organization_user",
                actor_id=actor_id,
                event_type="device.credentials_extended",
                details_json=json.dumps({
                    "credential_count": len(credentials),
                    "minimum_expires_at": minimum_expiry.isoformat(),
                }, sort_keys=True),
                created_at=extended_at,
            ))
            await session.commit()
            return tuple(
                self._device_credential_admin_record(credential, extended_at)
                for credential in credentials
            )

    @staticmethod
    def _config_proposal_record(
        proposal: OrganizationConfigProposal,
    ) -> OrganizationConfigProposalRecord:
        return OrganizationConfigProposalRecord(
            id=proposal.id,
            organization_id=proposal.organization_id,
            source_device_credential_id=proposal.source_device_credential_id,
            source_device_name=proposal.source_device_name,
            requested_by_user_id=proposal.requested_by_user_id,
            state=proposal.state,
            snapshot=json.loads(proposal.snapshot_json) if proposal.snapshot_json else {},
            diff=json.loads(proposal.diff_json) if proposal.diff_json else {},
        )

    @staticmethod
    def _config_release_record(
        release: OrganizationConfigRelease,
    ) -> OrganizationConfigReleaseRecord:
        return OrganizationConfigReleaseRecord(
            organization_id=release.organization_id,
            version_ms=release.version_ms,
            snapshot=json.loads(release.snapshot_json),
            source_device_credential_id=release.source_device_credential_id,
            source_device_name=release.source_device_name,
            approved_by_user_id=release.approved_by_user_id,
            comment=release.comment,
        )

    async def start_organization_config_proposal(
        self,
        *,
        organization_id: str,
        device_credential_id: str,
        requested_by_user_id: str,
        source_device_name: str = "",
    ) -> OrganizationConfigProposalRecord:
        async with self.sessions() as session:
            credential = await session.get(DeviceCredential, device_credential_id)
            user = await session.get(OrganizationUser, requested_by_user_id)
            if (
                credential is None
                or credential.organization_id != organization_id
                or credential.state != "active"
            ):
                raise ControlPlaneError("Active organization device not found.")
            if user is None or user.organization_id != organization_id:
                raise ControlPlaneError("Organization administrator not found.")
            proposal = await session.get(OrganizationConfigProposal, organization_id)
            if proposal is None:
                proposal = OrganizationConfigProposal(organization_id=organization_id)
                session.add(proposal)
            proposal.id = new_id()
            proposal.source_device_credential_id = device_credential_id
            proposal.source_device_name = source_device_name.strip()[:160] or credential.device_name
            proposal.requested_by_user_id = requested_by_user_id
            proposal.state = "awaiting_device"
            proposal.snapshot_json = ""
            proposal.diff_json = "{}"
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id,
                actor_type="organization_user",
                actor_id=requested_by_user_id,
                event_type="organization.config_pull_requested",
                details_json=json.dumps({"device_name": credential.device_name}),
            ))
            await session.commit()
            return self._config_proposal_record(proposal)

    async def complete_organization_config_proposal(
        self,
        *,
        proposal_id: str,
        organization_id: str,
        device_credential_id: str,
        snapshot_json: str,
        diff_json: str,
    ) -> OrganizationConfigProposalRecord:
        async with self.sessions() as session:
            proposal = await session.scalar(select(OrganizationConfigProposal).where(
                OrganizationConfigProposal.id == proposal_id,
                OrganizationConfigProposal.organization_id == organization_id,
            ))
            if proposal is None or proposal.source_device_credential_id != device_credential_id:
                raise ControlPlaneError("Configuration request not found for this device.")
            if proposal.state != "awaiting_device":
                raise ControlPlaneError("Configuration request is no longer awaiting a response.")
            proposal.snapshot_json = snapshot_json
            proposal.diff_json = diff_json
            proposal.state = "ready"
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id,
                actor_type="device_credential",
                actor_id=device_credential_id,
                event_type="organization.config_proposed",
                details_json=json.dumps({"device_name": proposal.source_device_name}),
            ))
            await session.commit()
            return self._config_proposal_record(proposal)

    async def get_organization_config_proposal(
        self, organization_id: str,
    ) -> Optional[OrganizationConfigProposalRecord]:
        async with self.sessions() as session:
            proposal = await session.get(OrganizationConfigProposal, organization_id)
            return self._config_proposal_record(proposal) if proposal else None

    async def reject_organization_config_proposal(
        self, *, organization_id: str, actor_user_id: str,
    ) -> None:
        async with self.sessions() as session:
            proposal = await session.get(OrganizationConfigProposal, organization_id)
            if proposal is None:
                raise ControlPlaneError("No proposed configuration is available.")
            await session.delete(proposal)
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id,
                actor_type="organization_user",
                actor_id=actor_user_id,
                event_type="organization.config_rejected",
                details_json=json.dumps({"device_name": proposal.source_device_name}),
            ))
            await session.commit()

    async def approve_organization_config_proposal(
        self,
        *,
        organization_id: str,
        actor_user_id: str,
        comment: str,
        now: Optional[datetime] = None,
    ) -> OrganizationConfigReleaseRecord:
        approved_at = now or utc_now()
        version_ms = int(approved_at.timestamp() * 1000)
        async with self.sessions() as session:
            proposal = await session.get(OrganizationConfigProposal, organization_id)
            if proposal is None or proposal.state != "ready" or not proposal.snapshot_json:
                raise ControlPlaneError("No completed configuration proposal is available.")
            release = OrganizationConfigRelease(
                organization_id=organization_id,
                version_ms=version_ms,
                snapshot_json=proposal.snapshot_json,
                content_sha256=hashlib.sha256(proposal.snapshot_json.encode("utf-8")).hexdigest(),
                source_device_credential_id=proposal.source_device_credential_id,
                source_device_name=proposal.source_device_name,
                approved_by_user_id=actor_user_id,
                comment=comment.strip()[:1000],
            )
            state = await session.get(OrganizationConfigState, organization_id)
            if state is None:
                state = OrganizationConfigState(organization_id=organization_id)
                session.add(state)
            state.current_version_ms = version_ms
            session.add(release)
            await session.delete(proposal)
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id,
                actor_type="organization_user",
                actor_id=actor_user_id,
                event_type="organization.config_approved",
                details_json=json.dumps({
                    "version_ms": version_ms,
                    "device_name": release.source_device_name,
                    "comment": release.comment,
                }),
                created_at=approved_at,
            ))
            await session.commit()
            return self._config_release_record(release)

    async def get_current_organization_config_release(
        self, organization_id: str,
    ) -> Optional[OrganizationConfigReleaseRecord]:
        async with self.sessions() as session:
            state = await session.get(OrganizationConfigState, organization_id)
            if state is None or state.current_version_ms == 0:
                return None
            release = await session.scalar(select(OrganizationConfigRelease).where(
                OrganizationConfigRelease.organization_id == organization_id,
                OrganizationConfigRelease.version_ms == state.current_version_ms,
            ))
            return self._config_release_record(release) if release else None

    async def get_organization_config_version_ms(self, organization_id: str) -> int:
        async with self.sessions() as session:
            state = await session.get(OrganizationConfigState, organization_id)
            return int(state.current_version_ms) if state else 0

    async def list_organization_config_releases(
        self, organization_id: str,
    ) -> tuple[OrganizationConfigReleaseRecord, ...]:
        async with self.sessions() as session:
            releases = (await session.scalars(select(OrganizationConfigRelease).where(
                OrganizationConfigRelease.organization_id == organization_id,
            ).order_by(OrganizationConfigRelease.version_ms.desc()))).all()
            return tuple(self._config_release_record(release) for release in releases)

    async def restore_organization_config_release(
        self, *, organization_id: str, version_ms: int, actor_user_id: str,
    ) -> OrganizationConfigReleaseRecord:
        async with self.sessions() as session:
            release = await session.scalar(select(OrganizationConfigRelease).where(
                OrganizationConfigRelease.organization_id == organization_id,
                OrganizationConfigRelease.version_ms == version_ms,
            ))
            if release is None:
                raise ControlPlaneError("Configuration release not found.")
            state = await session.get(OrganizationConfigState, organization_id)
            if state is None:
                state = OrganizationConfigState(organization_id=organization_id)
                session.add(state)
            state.current_version_ms = version_ms
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id,
                actor_type="organization_user",
                actor_id=actor_user_id,
                event_type="organization.config_restored",
                details_json=json.dumps({"version_ms": version_ms}),
            ))
            await session.commit()
            return self._config_release_record(release)

    async def advertise_video_stream(
        self,
        *,
        organization_id: str,
        device_credential_id: str,
        device_name: str = "",
        session_id: str,
        incident_name: str,
        drone_designator: str,
        source_width: int = 0,
        source_height: int = 0,
        source_fps: float = 0.0,
        source_bitrate_bps: int = 0,
        source_codec: str = "",
        media_kind: str = "live",
        recorded_at: Optional[datetime] = None,
        duration_ms: int = 0,
        thumbnail_revision: str = "",
        timezone_name: str = "UTC",
        remote_control_enabled: bool = False,
        ttl_seconds: int = 45,
        now: Optional[datetime] = None,
    ) -> ActiveVideoStreamRecord:
        seen_at = now or utc_now()
        clean_session_id = session_id.strip()
        clean_incident = incident_name.strip()
        clean_drone = drone_designator.strip()
        clean_device_name = device_name.strip()
        clean_codec = source_codec.strip().lower()
        clean_media_kind = media_kind.strip().lower()
        if clean_media_kind not in {"live", "recording"}:
            raise ControlPlaneError("Video media kind must be live or recording.")
        clean_thumbnail_revision = thumbnail_revision.strip()
        if len(clean_thumbnail_revision) > 64:
            raise ControlPlaneError("Thumbnail revision must be 64 characters or fewer.")
        clean_recorded_at = as_utc(recorded_at) if recorded_at is not None else None
        clean_duration_ms = max(0, min(int(duration_ms), 24 * 60 * 60 * 1000))
        clean_timezone = normalize_timezone_name(timezone_name)
        if not clean_session_id or len(clean_session_id) > 36:
            raise ControlPlaneError("A valid stream session ID is required.")
        if not clean_incident:
            raise ControlPlaneError("An incident name is required.")
        if not clean_drone:
            raise ControlPlaneError("A drone designator is required.")
        if len(clean_device_name) > 160:
            raise ControlPlaneError("Device name must be 160 characters or fewer.")
        if not 10 <= ttl_seconds <= 120:
            raise ControlPlaneError("Stream presence TTL must be 10-120 seconds.")
        width = max(0, min(int(source_width), 16384))
        height = max(0, min(int(source_height), 16384))
        fps_milli = max(0, min(int(round(source_fps * 1000)), 240000))
        bitrate = max(0, min(int(source_bitrate_bps), 1_000_000_000))
        expires_at = seen_at + timedelta(seconds=ttl_seconds)
        async with self.sessions() as session:
            credential = await session.get(DeviceCredential, device_credential_id)
            if (
                credential is None
                or credential.organization_id != organization_id
                or credential.state != "active"
            ):
                raise ControlPlaneError("Active organization device not found.")
            stream = await session.scalar(
                select(ActiveVideoStream).where(
                    ActiveVideoStream.session_id == clean_session_id
                )
            )
            if stream is not None and (
                stream.organization_id != organization_id
                or stream.device_credential_id != device_credential_id
            ):
                raise ControlPlaneError(
                    "Stream session belongs to a different organization device."
                )
            is_new_stream = stream is None
            if stream is None:
                stream = ActiveVideoStream(
                    id=new_id(),
                    session_id=clean_session_id,
                    organization_id=organization_id,
                    device_credential_id=device_credential_id,
                    created_at=seen_at,
                )
                session.add(stream)
            if clean_device_name:
                credential.device_name = clean_device_name
            meaningful_change = is_new_stream or any((
                stream.state != "active",
                as_utc(stream.expires_at) is None,
                (as_utc(stream.expires_at) or seen_at) < seen_at,
                stream.incident_name != clean_incident[:160],
                stream.drone_designator != clean_drone[:160],
                stream.device_name != credential.device_name[:160],
                stream.media_kind != clean_media_kind,
                as_utc(stream.recorded_at) != clean_recorded_at,
                stream.duration_ms != clean_duration_ms,
                stream.thumbnail_revision != clean_thumbnail_revision,
                stream.timezone_name != clean_timezone,
                stream.remote_control_enabled != bool(remote_control_enabled),
            ))
            stream.incident_name = clean_incident[:160]
            stream.drone_designator = clean_drone[:160]
            stream.device_name = credential.device_name[:160]
            if is_new_stream or width > 0:
                stream.source_width = width
            if is_new_stream or height > 0:
                stream.source_height = height
            if is_new_stream or fps_milli > 0:
                stream.source_fps_milli = fps_milli
            if is_new_stream or bitrate > 0:
                stream.source_bitrate_bps = bitrate
            if is_new_stream or clean_codec:
                stream.source_codec = clean_codec[:32]
            stream.media_kind = clean_media_kind
            stream.recorded_at = clean_recorded_at
            stream.duration_ms = clean_duration_ms
            stream.thumbnail_revision = clean_thumbnail_revision
            stream.timezone_name = clean_timezone
            stream.remote_control_enabled = bool(remote_control_enabled)
            stream.state = "active"
            stream.last_seen_at = seen_at
            stream.expires_at = expires_at
            if meaningful_change:
                await self._notify_video_stream_change(session, organization_id)
            await session.commit()
            return self._active_video_stream_record(stream)

    async def list_active_video_streams(
        self,
        organization_id: str,
        now: Optional[datetime] = None,
    ) -> tuple[ActiveVideoStreamRecord, ...]:
        checked_at = now or utc_now()
        async with self.sessions() as session:
            streams = (
                await session.scalars(
                    select(ActiveVideoStream)
                    .where(
                        ActiveVideoStream.organization_id == organization_id,
                        ActiveVideoStream.state == "active",
                        ActiveVideoStream.expires_at >= checked_at,
                    )
                    .order_by(
                        func.lower(ActiveVideoStream.incident_name),
                        func.lower(ActiveVideoStream.drone_designator),
                    )
                )
            ).all()
        return tuple(self._active_video_stream_record(stream) for stream in streams)

    async def reconcile_device_video_streams(
        self,
        *,
        organization_id: str,
        device_credential_id: str,
        active_session_ids: Iterable[str],
        notify_even_if_unchanged: bool = False,
        now: Optional[datetime] = None,
    ) -> int:
        """Retire streams omitted from a device's authoritative advertisement."""
        retired_at = now or utc_now()
        clean_session_ids = {
            str(session_id).strip()
            for session_id in active_session_ids
            if str(session_id).strip()
        }
        async with self.sessions() as session:
            statement = select(ActiveVideoStream).where(
                ActiveVideoStream.organization_id == organization_id,
                ActiveVideoStream.device_credential_id == device_credential_id,
                ActiveVideoStream.state == "active",
            )
            if clean_session_ids:
                statement = statement.where(
                    ActiveVideoStream.session_id.not_in(clean_session_ids)
                )
            streams = (await session.scalars(statement)).all()
            for stream in streams:
                stream.state = "inactive"
                stream.expires_at = retired_at
            retired_stream_ids = [stream.id for stream in streams]
            unavailable_requests = []
            if retired_stream_ids:
                unavailable_requests = (
                    await session.scalars(
                        select(VideoStreamRequest).where(
                            VideoStreamRequest.active_stream_id.in_(
                                retired_stream_ids
                            ),
                            VideoStreamRequest.state.in_((
                                "pending",
                                "probing",
                                "awaiting_approval",
                                "approved",
                                "streaming",
                            )),
                        )
                    )
                ).all()
                for request in unavailable_requests:
                    request.state = "e_nosuch_stream"
                    request.stopped_at = retired_at
                    session.add(
                        ControlPlaneAuditEvent(
                            organization_id=request.organization_id,
                            actor_type="organization_device",
                            actor_id=device_credential_id,
                            event_type="video.e_nosuch_stream",
                            details_json=json.dumps({"request_id": request.id}),
                            created_at=retired_at,
                        )
                    )
                request_ids = [request.id for request in unavailable_requests]
                if request_ids:
                    await session.execute(
                        delete(VideoPreflightExchange).where(
                            VideoPreflightExchange.request_id.in_(request_ids)
                        )
                    )
                    await session.execute(
                        delete(VideoMediaExchange).where(
                            VideoMediaExchange.request_id.in_(request_ids)
                        )
                    )
            if streams or notify_even_if_unchanged:
                await self._notify_video_stream_change(session, organization_id)
                await session.commit()
            return len(streams)

    async def create_video_stream_request(
        self,
        *,
        organization_id: str,
        stream_session_id: str,
        requester_user_id: str,
        now: Optional[datetime] = None,
    ) -> VideoStreamRequestRecord:
        requested_at = now or utc_now()
        async with self.sessions() as session:
            allowance = await session.scalar(
                select(ExtendedBetaAllowance).where(
                    ExtendedBetaAllowance.organization_id == organization_id,
                    ExtendedBetaAllowance.billing_month == requested_at.strftime("%Y-%m"),
                    ExtendedBetaAllowance.video_disabled_at.is_not(None),
                )
            )
            if allowance is not None:
                raise ControlPlaneError(
                    "Remote video streaming is disabled for the remainder of "
                    f"the month ending {as_utc(allowance.month_ends_at).strftime('%d %b %Y')}. "
                    "Flight logs and R2C-based drone-owner arbitration remain available."
                )
            stream = await session.scalar(
                select(ActiveVideoStream).where(
                    ActiveVideoStream.organization_id == organization_id,
                    ActiveVideoStream.session_id == stream_session_id,
                    ActiveVideoStream.state == "active",
                    ActiveVideoStream.expires_at >= requested_at,
                ).with_for_update()
            )
            requester = await session.get(OrganizationUser, requester_user_id)
            if stream is None:
                raise ControlPlaneError(
                    "That stream is no longer active. Refresh the list and try again."
                )
            if (
                requester is None
                or requester.organization_id != organization_id
                or requester.state != "active"
                or "video_requester" not in requester.roles
            ):
                raise ControlPlaneError(
                    "Your organization role does not permit stream requests."
                )
            pending_device_request = await session.scalar(
                select(VideoStreamRequest)
                .join(
                    ActiveVideoStream,
                    ActiveVideoStream.id == VideoStreamRequest.active_stream_id,
                )
                .where(
                    ActiveVideoStream.device_credential_id
                    == stream.device_credential_id,
                    VideoStreamRequest.state.in_((
                        "pending",
                        "probing",
                        "awaiting_approval",
                    )),
                    VideoStreamRequest.expires_at >= requested_at,
                )
                .with_for_update()
            )
            if pending_device_request is not None:
                raise ControlPlaneError(
                    "A video request is already in progress and awaiting review "
                    "on this R2C app."
                )
            if stream.remote_control_enabled:
                active_consumer = await session.scalar(
                    select(VideoStreamRequest.id)
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id == VideoStreamRequest.active_stream_id,
                    )
                    .where(
                        ActiveVideoStream.device_credential_id
                        == stream.device_credential_id,
                        VideoStreamRequest.state.in_(("approved", "streaming")),
                    )
                    .with_for_update()
                )
                if active_consumer is not None:
                    raise ControlPlaneError(
                        "This R2C app is already sharing video with another member."
                    )
            request = VideoStreamRequest(
                id=new_id(),
                organization_id=organization_id,
                active_stream_id=stream.id,
                requester_user_id=requester.id,
                requester_email=requester.email,
                remote_control_enabled=bool(stream.remote_control_enabled),
                state="pending",
                requested_at=requested_at,
                expires_at=requested_at + timedelta(
                    seconds=VIDEO_RESPONSE_TIMEOUT_SECONDS
                ),
            )
            session.add(request)
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=organization_id,
                    actor_type="organization_user",
                    actor_id=requester.id,
                    event_type="video.requested",
                    # Platform-level audit views may show that a request
                    # occurred, but never incident or aircraft labels.
                    details_json=json.dumps({"request_id": request.id}),
                    created_at=requested_at,
                )
            )
            if self.engine.dialect.name == "postgresql":
                await session.execute(
                    select(func.pg_notify("r2c_video_request", request.id))
                )
            await self._notify_video_stream_change(session, organization_id)
            await session.commit()
            return self._video_stream_request_record(request, stream)

    async def create_recording_download_request(
        self, *, organization_id: str, stream_session_id: str,
        requester_user_id: str, now: Optional[datetime] = None,
    ) -> RecordingDownloadRequestRecord:
        requested_at = now or utc_now()
        async with self.sessions() as session:
            row = (await session.execute(
                select(ActiveVideoStream, OrganizationUser)
                .join(OrganizationUser, OrganizationUser.id == requester_user_id)
                .where(
                    ActiveVideoStream.organization_id == organization_id,
                    ActiveVideoStream.session_id == stream_session_id,
                    ActiveVideoStream.media_kind == "recording",
                    ActiveVideoStream.state == "active",
                    ActiveVideoStream.expires_at >= requested_at,
                    OrganizationUser.organization_id == organization_id,
                    OrganizationUser.state == "active",
                ).with_for_update()
            )).first()
            if row is None or "video_requester" not in row[1].roles:
                raise ControlPlaneError("Captured recording is unavailable or unauthorized.")
            stream, requester = row
            ready = await session.scalar(select(RecordingDownloadRequest).where(
                RecordingDownloadRequest.active_stream_id == stream.id,
                RecordingDownloadRequest.state == "ready",
            ).order_by(RecordingDownloadRequest.completed_at.desc()))
            if ready is not None:
                return self._recording_download_request_record(ready, stream)
            pending = await session.scalar(select(RecordingDownloadRequest).where(
                RecordingDownloadRequest.active_stream_id == stream.id,
                RecordingDownloadRequest.requester_user_id == requester.id,
                RecordingDownloadRequest.state.in_(("awaiting_approval", "approved", "uploading")),
                RecordingDownloadRequest.expires_at >= requested_at,
            ))
            if pending is not None:
                return self._recording_download_request_record(pending, stream)
            item = RecordingDownloadRequest(
                id=new_id(), organization_id=organization_id,
                active_stream_id=stream.id, requester_user_id=requester.id,
                requester_email=requester.email,
                device_credential_id=stream.device_credential_id,
                remote_control_enabled=bool(stream.remote_control_enabled),
                state=("approved" if stream.remote_control_enabled else "awaiting_approval"),
                requested_at=requested_at,
                expires_at=requested_at + timedelta(
                    seconds=(
                        RECORDING_TRANSFER_TIMEOUT_SECONDS
                        if stream.remote_control_enabled
                        else RECORDING_APPROVAL_TIMEOUT_SECONDS
                    )
                ),
            )
            session.add(item)
            session.add(ControlPlaneAuditEvent(
                organization_id=organization_id, actor_type="organization_user",
                actor_id=requester.id, event_type="recording.download_requested",
                details_json=json.dumps({"request_id": item.id}), created_at=requested_at,
            ))
            if self.engine.dialect.name == "postgresql":
                await session.execute(select(func.pg_notify("r2c_recording_download", item.id)))
            await self._notify_video_stream_change(session, organization_id)
            await session.commit()
            return self._recording_download_request_record(item, stream)

    async def get_recording_download_request(
        self, *, request_id: str, organization_id: Optional[str] = None,
        requester_user_id: Optional[str] = None,
        device_credential_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> RecordingDownloadRequestRecord:
        checked_at = now or utc_now()
        statement = (select(RecordingDownloadRequest, ActiveVideoStream)
            .join(ActiveVideoStream, ActiveVideoStream.id == RecordingDownloadRequest.active_stream_id)
            .where(RecordingDownloadRequest.id == request_id))
        if organization_id is not None:
            statement = statement.where(RecordingDownloadRequest.organization_id == organization_id)
        if requester_user_id is not None:
            statement = statement.where(RecordingDownloadRequest.requester_user_id == requester_user_id)
        if device_credential_id is not None:
            statement = statement.where(RecordingDownloadRequest.device_credential_id == device_credential_id)
        async with self.sessions() as session:
            await self._expire_recording_download_requests(
                session,
                checked_at=checked_at,
                organization_id=organization_id,
                request_id=request_id,
            )
            await session.commit()
            row = (await session.execute(statement)).first()
        if row is None:
            raise ControlPlaneError("Recording download request was not found.")
        return self._recording_download_request_record(*row)

    async def list_recording_download_requests(
        self, *, organization_id: str, requester_user_id: Optional[str] = None,
        limit: int = 100, now: Optional[datetime] = None,
    ) -> tuple[RecordingDownloadRequestRecord, ...]:
        checked_at = now or utc_now()
        statement = (select(RecordingDownloadRequest, ActiveVideoStream)
            .join(ActiveVideoStream, ActiveVideoStream.id == RecordingDownloadRequest.active_stream_id)
            .where(RecordingDownloadRequest.organization_id == organization_id))
        if requester_user_id is not None:
            statement = statement.where(
                RecordingDownloadRequest.requester_user_id == requester_user_id
            )
        async with self.sessions() as session:
            await self._expire_recording_download_requests(
                session,
                checked_at=checked_at,
                organization_id=organization_id,
            )
            await session.commit()
            rows = (await session.execute(statement
                .order_by(RecordingDownloadRequest.requested_at.desc())
                .limit(max(1, min(limit, 200))))).all()
        return tuple(self._recording_download_request_record(*row) for row in rows)

    async def list_pending_recording_download_requests_for_device(
        self,
        *,
        device_credential_id: str,
        now: Optional[datetime] = None,
        limit: int = 20,
    ) -> tuple[RecordingDownloadRequestRecord, ...]:
        checked_at = now or utc_now()
        statement = (
            select(RecordingDownloadRequest, ActiveVideoStream)
            .join(
                ActiveVideoStream,
                ActiveVideoStream.id
                == RecordingDownloadRequest.active_stream_id,
            )
            .where(
                RecordingDownloadRequest.device_credential_id
                == device_credential_id,
                RecordingDownloadRequest.state.in_((
                    "awaiting_approval", "approved",
                )),
                RecordingDownloadRequest.expires_at >= checked_at,
            )
            .order_by(RecordingDownloadRequest.requested_at)
            .limit(max(1, min(limit, 50)))
        )
        async with self.sessions() as session:
            await self._expire_recording_download_requests(
                session,
                checked_at=checked_at,
            )
            await session.commit()
            rows = (await session.execute(statement)).all()
        return tuple(
            self._recording_download_request_record(item, stream)
            for item, stream in rows
        )

    async def decide_recording_download_request(
        self, *, request_id: str, device_credential_id: str, approved: bool,
        now: Optional[datetime] = None,
    ) -> RecordingDownloadRequestRecord:
        decided_at = now or utc_now()
        async with self.sessions() as session:
            row = (await session.execute(
                select(RecordingDownloadRequest, ActiveVideoStream)
                .join(ActiveVideoStream, ActiveVideoStream.id == RecordingDownloadRequest.active_stream_id)
                .where(RecordingDownloadRequest.id == request_id).with_for_update()
            )).first()
            if row is None or row[0].device_credential_id != device_credential_id:
                raise ControlPlaneError("Recording download request was not found.")
            item, stream = row
            if as_utc(item.expires_at) < decided_at:
                item.state = "expired"
                item.status_message = "Recording transfer request timed out."
                await self._notify_video_stream_change(session, item.organization_id)
                await session.commit()
                raise ControlPlaneError("Recording download request has expired.")
            if approved and item.state in {"uploading", "ready"}:
                # The authenticated HTTP upload is itself an authoritative
                # approval.  A small or fast first chunk can therefore reach
                # us before the websocket decision.  Acknowledge that delayed
                # decision idempotently instead of turning a valid transfer
                # into a rejected approval.
                return self._recording_download_request_record(item, stream)
            if item.state not in {"awaiting_approval", "approved"}:
                raise ControlPlaneError("Recording download request is no longer awaiting a decision.")
            item.state = "approved" if approved else "declined"
            item.status_message = (
                "Recording transfer approved; waiting for the tablet to upload it."
                if approved
                else "Tablet operator declined the transfer."
            )
            item.expires_at = decided_at + timedelta(
                seconds=RECORDING_TRANSFER_TIMEOUT_SECONDS
            ) if approved else decided_at
            await self._notify_video_stream_change(session, item.organization_id)
            await session.commit()
            return self._recording_download_request_record(item, stream)

    async def complete_recording_download_upload(
        self, *, request_id: str, device_credential_id: str, filename: str,
        media_type: str, byte_count: int, sha256: str, storage_relpath: str,
        spool_ttl_seconds: int = 3600,
    ) -> RecordingDownloadRequestRecord:
        completed_at = utc_now()
        async with self.sessions() as session:
            row = (await session.execute(
                select(RecordingDownloadRequest, ActiveVideoStream)
                .join(ActiveVideoStream, ActiveVideoStream.id == RecordingDownloadRequest.active_stream_id)
                .where(RecordingDownloadRequest.id == request_id).with_for_update()
            )).first()
            if row is None or row[0].device_credential_id != device_credential_id:
                raise ControlPlaneError("Recording download request was not found.")
            item, stream = row
            if as_utc(item.expires_at) < completed_at:
                item.state = "expired"
                item.status_message = "Recording transfer timed out."
                await self._notify_video_stream_change(session, item.organization_id)
                await session.commit()
                raise ControlPlaneError("Recording transfer has expired.")
            if item.state not in {"approved", "uploading"}:
                raise ControlPlaneError("Recording transfer is not authorized.")
            item.state = "ready"
            item.status_message = "Recording is ready to download."
            item.filename = filename[:240]
            item.media_type = media_type[:120]
            item.byte_count = max(0, byte_count)
            item.sha256 = sha256[:64]
            item.storage_relpath = storage_relpath[:500]
            item.completed_at = completed_at
            # The completed file is a short-lived transfer spool, not tracker
            # media storage. The browser normally consumes it immediately.
            item.expires_at = completed_at + timedelta(
                seconds=max(60, spool_ttl_seconds)
            )
            session.add(ControlPlaneAuditEvent(
                organization_id=item.organization_id, actor_type="organization_device",
                actor_id=device_credential_id, event_type="recording.download_ready",
                details_json=json.dumps({"request_id": item.id, "bytes": item.byte_count}),
                created_at=completed_at,
            ))
            await self._notify_video_stream_change(session, item.organization_id)
            await session.commit()
            return self._recording_download_request_record(item, stream)

    async def mark_recording_download_uploading(
        self,
        *,
        request_id: str,
        device_credential_id: str,
        now: Optional[datetime] = None,
    ) -> RecordingDownloadRequestRecord:
        started_at = now or utc_now()
        async with self.sessions() as session:
            row = (await session.execute(
                select(RecordingDownloadRequest, ActiveVideoStream)
                .join(
                    ActiveVideoStream,
                    ActiveVideoStream.id
                    == RecordingDownloadRequest.active_stream_id,
                )
                .where(RecordingDownloadRequest.id == request_id)
                .with_for_update()
            )).first()
            if row is None or row[0].device_credential_id != device_credential_id:
                raise ControlPlaneError("Recording download request was not found.")
            item, stream = row
            if as_utc(item.expires_at) < started_at:
                item.state = "expired"
                item.status_message = "Recording transfer timed out."
                await self._notify_video_stream_change(
                    session, item.organization_id
                )
                await session.commit()
                raise ControlPlaneError("Recording transfer has expired.")
            if item.state not in {"approved", "uploading"}:
                raise ControlPlaneError("Recording transfer is not authorized.")
            if item.state == "approved":
                item.state = "uploading"
                item.status_message = "Recording transfer is in progress."
                await self._notify_video_stream_change(
                    session, item.organization_id
                )
                await session.commit()
            return self._recording_download_request_record(item, stream)

    async def _expire_recording_download_requests(
        self,
        session: AsyncSession,
        *,
        checked_at: datetime,
        organization_id: Optional[str] = None,
        request_id: str = "",
    ) -> int:
        statement = select(RecordingDownloadRequest).where(
            RecordingDownloadRequest.state.in_((
                "awaiting_approval", "approved", "uploading"
            )),
            RecordingDownloadRequest.expires_at < checked_at,
        )
        if organization_id is not None:
            statement = statement.where(
                RecordingDownloadRequest.organization_id == organization_id
            )
        if request_id:
            statement = statement.where(
                RecordingDownloadRequest.id == request_id
            )
        items = tuple((await session.scalars(statement)).all())
        for item in items:
            prior_state = item.state
            item.state = "expired"
            item.status_message = (
                "Recording transfer request timed out."
                if prior_state == "awaiting_approval"
                else "Recording transfer timed out."
            )
        for expired_organization_id in {
            item.organization_id for item in items
        }:
            await self._notify_video_stream_change(
                session, expired_organization_id
            )
        return len(items)

    async def complete_recording_download_delivery(
        self, *, request_id: str, now: Optional[datetime] = None,
    ) -> RecordingDownloadRequestRecord:
        delivered_at = now or utc_now()
        async with self.sessions() as session:
            row = (await session.execute(
                select(RecordingDownloadRequest, ActiveVideoStream)
                .join(ActiveVideoStream, ActiveVideoStream.id == RecordingDownloadRequest.active_stream_id)
                .where(RecordingDownloadRequest.id == request_id).with_for_update()
            )).first()
            if row is None:
                raise ControlPlaneError("Recording download request was not found.")
            item, stream = row
            if item.state == "ready":
                item.state = "downloaded"
                item.status_message = "Recording download completed."
                item.storage_relpath = ""
                item.expires_at = delivered_at
                session.add(ControlPlaneAuditEvent(
                    organization_id=item.organization_id,
                    actor_type="organization_user",
                    actor_id=item.requester_user_id,
                    event_type="recording.download_completed",
                    details_json=json.dumps({"request_id": item.id, "bytes": item.byte_count}),
                    created_at=delivered_at,
                ))
                await self._notify_video_stream_change(session, item.organization_id)
                await session.commit()
            return self._recording_download_request_record(item, stream)

    async def expire_recording_download_spools(
        self, *, now: Optional[datetime] = None,
    ) -> tuple[str, ...]:
        """Expire abandoned transfer spools and return their relative paths."""
        checked_at = now or utc_now()
        async with self.sessions() as session:
            items = (await session.execute(
                select(RecordingDownloadRequest).where(
                    RecordingDownloadRequest.state == "ready",
                    RecordingDownloadRequest.expires_at < checked_at,
                    RecordingDownloadRequest.storage_relpath != "",
                ).with_for_update()
            )).scalars().all()
            paths = tuple(item.storage_relpath for item in items if item.storage_relpath)
            for item in items:
                item.state = "expired"
                item.status_message = "Recording transfer expired before download."
                item.storage_relpath = ""
            await session.commit()
            return paths

    async def get_pending_video_stream_request(
        self,
        request_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[VideoStreamRequestRecord]:
        checked_at = now or utc_now()
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(VideoStreamRequest, ActiveVideoStream)
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id
                        == VideoStreamRequest.active_stream_id,
                    )
                    .where(
                        VideoStreamRequest.id == request_id,
                        VideoStreamRequest.state.in_(("pending", "probing")),
                        VideoStreamRequest.expires_at >= checked_at,
                    )
                )
            ).first()
        if row is None:
            return None
        request, stream = row
        return self._video_stream_request_record(request, stream)

    async def list_video_stream_requests(
        self,
        *,
        organization_id: str,
        requester_user_id: Optional[str] = None,
        limit: int = 20,
        now: Optional[datetime] = None,
    ) -> tuple[VideoStreamRequestRecord, ...]:
        checked_at = now or utc_now()
        statement = (
            select(VideoStreamRequest, ActiveVideoStream)
            .join(
                ActiveVideoStream,
                ActiveVideoStream.id == VideoStreamRequest.active_stream_id,
            )
            .where(VideoStreamRequest.organization_id == organization_id)
        )
        if requester_user_id is not None:
            statement = statement.where(
                VideoStreamRequest.requester_user_id == requester_user_id
            )
        statement = statement.order_by(
            VideoStreamRequest.requested_at.desc()
        ).limit(max(1, min(limit, 100)))
        async with self.sessions() as session:
            await self._expire_video_stream_requests(
                session,
                checked_at=checked_at,
                organization_id=organization_id,
            )
            await session.execute(
                delete(VideoPreflightExchange).where(
                    VideoPreflightExchange.expires_at < checked_at
                )
            )
            await session.commit()
            rows = (await session.execute(statement)).all()
        return tuple(
            self._video_stream_request_record(request, stream)
            for request, stream in rows
        )

    async def get_video_stream_request_for_requester(
        self, *, request_id: str, organization_id: str, requester_user_id: str,
        now: Optional[datetime] = None,
    ) -> VideoStreamRequestRecord:
        checked_at = now or utc_now()
        async with self.sessions() as session:
            await self._expire_video_stream_requests(
                session,
                checked_at=checked_at,
                organization_id=organization_id,
            )
            await session.commit()
            row = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream)
                .join(
                    ActiveVideoStream,
                    ActiveVideoStream.id == VideoStreamRequest.active_stream_id,
                )
                .where(
                    VideoStreamRequest.id == request_id,
                    VideoStreamRequest.organization_id == organization_id,
                    VideoStreamRequest.requester_user_id == requester_user_id,
                )
            )).first()
        if row is None:
            raise ControlPlaneError("Video stream request was not found.")
        return self._video_stream_request_record(*row)

    async def list_pending_video_stream_requests_for_device(
        self,
        *,
        device_credential_id: str,
        now: Optional[datetime] = None,
        limit: int = 20,
    ) -> tuple[VideoStreamRequestRecord, ...]:
        checked_at = now or utc_now()
        statement = (
            select(VideoStreamRequest, ActiveVideoStream)
            .join(
                ActiveVideoStream,
                ActiveVideoStream.id == VideoStreamRequest.active_stream_id,
            )
            .where(
                ActiveVideoStream.device_credential_id == device_credential_id,
                VideoStreamRequest.state.in_(("pending", "probing")),
                VideoStreamRequest.expires_at >= checked_at,
            )
            .order_by(VideoStreamRequest.requested_at)
            .limit(max(1, min(limit, 100)))
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).all()
        return tuple(
            self._video_stream_request_record(request, stream)
            for request, stream in rows
        )

    async def cancel_video_stream_request(
        self,
        *,
        request_id: str,
        organization_id: str,
        requester_user_id: str,
        now: Optional[datetime] = None,
    ) -> VideoStreamRequestRecord:
        cancelled_at = now or utc_now()
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(VideoStreamRequest, ActiveVideoStream)
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id == VideoStreamRequest.active_stream_id,
                    )
                    .where(VideoStreamRequest.id == request_id)
                )
            ).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream = row
            if (
                request.organization_id != organization_id
                or request.requester_user_id != requester_user_id
            ):
                raise ControlPlaneError(
                    "Video stream request belongs to another user."
                )
            if as_utc(request.expires_at) < cancelled_at:
                request.state = "expired"
                request.stopped_at = cancelled_at
                exchange = await session.get(VideoPreflightExchange, request.id)
                if exchange is not None:
                    await session.delete(exchange)
                await session.commit()
                raise ControlPlaneError("Video stream request has expired.")
            if request.state == "cancelled":
                return self._video_stream_request_record(request, stream)
            if request.state not in {"pending", "probing", "awaiting_approval"}:
                raise ControlPlaneError(
                    "Video stream request can no longer be cancelled."
                )
            request.state = "cancelled"
            request.stopped_at = cancelled_at
            exchange = await session.get(VideoPreflightExchange, request.id)
            if exchange is not None:
                await session.delete(exchange)
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=request.organization_id,
                    actor_type="organization_user",
                    actor_id=requester_user_id,
                    event_type="video.request_cancelled",
                    details_json=json.dumps({"request_id": request.id}),
                    created_at=cancelled_at,
                )
            )
            await self._notify_video_stream_change(
                session, request.organization_id
            )
            await session.commit()
            return self._video_stream_request_record(request, stream)

    async def _expire_video_stream_requests(
        self,
        session: AsyncSession,
        *,
        checked_at: datetime,
        organization_id: Optional[str] = None,
    ) -> int:
        statement = select(
            VideoStreamRequest.id,
            VideoStreamRequest.organization_id,
        ).where(
            VideoStreamRequest.state.in_((
                "pending", "probing", "awaiting_approval", "approved",
                "streaming",
            )),
            VideoStreamRequest.expires_at < checked_at,
        )
        if organization_id is not None:
            statement = statement.where(
                VideoStreamRequest.organization_id == organization_id
            )
        request_rows = tuple((await session.execute(statement)).all())
        request_ids = tuple(row[0] for row in request_rows)
        if not request_ids:
            return 0
        await session.execute(
            update(VideoStreamRequest)
            .where(VideoStreamRequest.id.in_(request_ids))
            .values(
                state="expired",
                status_message="Video request timed out.",
                stopped_at=checked_at,
            )
        )
        await session.execute(
            delete(VideoPreflightExchange).where(
                VideoPreflightExchange.request_id.in_(request_ids)
            )
        )
        await session.execute(
            delete(VideoMediaExchange).where(
                VideoMediaExchange.request_id.in_(request_ids)
            )
        )
        for expired_organization_id in {row[1] for row in request_rows}:
            await self._notify_video_stream_change(
                session, expired_organization_id
            )
        return len(request_ids)

    async def start_video_preflight(
        self,
        *,
        request_id: str,
        organization_id: str,
        requester_user_id: str,
        browser_offer_sdp: str,
        relay_candidate_ms: int = 0,
        now: Optional[datetime] = None,
    ) -> VideoPreflightExchangeRecord:
        started_at = now or utc_now()
        offer = normalize_session_description(browser_offer_sdp)
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(VideoStreamRequest, ActiveVideoStream)
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id
                        == VideoStreamRequest.active_stream_id,
                    )
                    .where(VideoStreamRequest.id == request_id)
                )
            ).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream = row
            if (
                request.organization_id != organization_id
                or request.requester_user_id != requester_user_id
            ):
                raise ControlPlaneError(
                    "Video stream request belongs to another user."
                )
            if as_utc(request.expires_at) < started_at:
                request.state = "expired"
                await session.commit()
                raise ControlPlaneError("Video stream request has expired.")
            if request.state not in {"pending", "probing"}:
                raise ControlPlaneError(
                    "Video stream request is not available for preflight."
                )
            exchange = await session.get(VideoPreflightExchange, request.id)
            if exchange is None:
                exchange = VideoPreflightExchange(
                    request_id=request.id,
                    browser_offer_sdp=offer,
                    device_answer_sdp="",
                    created_at=started_at,
                    updated_at=started_at,
                    expires_at=request.expires_at,
                )
                session.add(exchange)
            else:
                exchange.browser_offer_sdp = offer
                exchange.device_answer_sdp = ""
                exchange.updated_at = started_at
                exchange.expires_at = request.expires_at
            request.state = "probing"
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=request.organization_id,
                    actor_type="organization_user",
                    actor_id=requester_user_id,
                    event_type="video.preflight_started",
                    details_json=json.dumps({
                        "request_id": request.id,
                        "browser_relay_candidate_ms": max(
                            0, min(int(relay_candidate_ms), 60_000)
                        ),
                    }),
                    created_at=started_at,
                )
            )
            if self.engine.dialect.name == "postgresql":
                await session.execute(
                    select(func.pg_notify("r2c_video_preflight", request.id))
                )
            await self._notify_video_stream_change(
                session, request.organization_id
            )
            await session.commit()
            return self._video_preflight_exchange_record(
                request,
                stream,
                exchange,
            )

    async def get_video_preflight_exchange_for_requester(
        self,
        *,
        request_id: str,
        organization_id: str,
        requester_user_id: str,
        now: Optional[datetime] = None,
    ) -> VideoPreflightExchangeRecord:
        checked_at = now or utc_now()
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(
                        VideoStreamRequest,
                        ActiveVideoStream,
                        VideoPreflightExchange,
                    )
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id
                        == VideoStreamRequest.active_stream_id,
                    )
                    .outerjoin(
                        VideoPreflightExchange,
                        VideoPreflightExchange.request_id
                        == VideoStreamRequest.id,
                    )
                    .where(VideoStreamRequest.id == request_id)
                )
            ).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream, exchange = row
            if (
                request.organization_id != organization_id
                or request.requester_user_id != requester_user_id
            ):
                raise ControlPlaneError(
                    "Video stream request belongs to another user."
                )
            if as_utc(request.expires_at) < checked_at:
                request.state = "expired"
                if exchange is not None:
                    await session.delete(exchange)
                await session.commit()
                raise ControlPlaneError("Video stream request has expired.")
            if exchange is None:
                exchange = VideoPreflightExchange(
                    request_id=request.id,
                    browser_offer_sdp="",
                    device_answer_sdp="",
                    created_at=request.requested_at,
                    updated_at=request.requested_at,
                    expires_at=request.expires_at,
                )
            return self._video_preflight_exchange_record(
                request,
                stream,
                exchange,
            )

    async def cleanup_expired_video_preflight_exchanges(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """Discard expired SDP and embedded ICE credentials."""
        checked_at = now or utc_now()
        async with self.sessions() as session:
            await self._expire_video_stream_requests(
                session,
                checked_at=checked_at,
            )
            result = await session.execute(
                delete(VideoPreflightExchange).where(
                    VideoPreflightExchange.expires_at < checked_at
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def get_pending_video_preflight_offer(
        self,
        *,
        request_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[VideoPreflightExchangeRecord]:
        checked_at = now or utc_now()
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(
                        VideoStreamRequest,
                        ActiveVideoStream,
                        VideoPreflightExchange,
                    )
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id
                        == VideoStreamRequest.active_stream_id,
                    )
                    .join(
                        VideoPreflightExchange,
                        VideoPreflightExchange.request_id
                        == VideoStreamRequest.id,
                    )
                    .where(
                        VideoStreamRequest.id == request_id,
                        VideoStreamRequest.state == "probing",
                        VideoStreamRequest.expires_at >= checked_at,
                    )
                )
            ).first()
        if row is None:
            return None
        request, stream, exchange = row
        return self._video_preflight_exchange_record(
            request,
            stream,
            exchange,
        )

    async def list_pending_video_preflight_offers_for_device(
        self,
        *,
        device_credential_id: str,
        now: Optional[datetime] = None,
        limit: int = 10,
    ) -> tuple[VideoPreflightExchangeRecord, ...]:
        checked_at = now or utc_now()
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        VideoStreamRequest,
                        ActiveVideoStream,
                        VideoPreflightExchange,
                    )
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id
                        == VideoStreamRequest.active_stream_id,
                    )
                    .join(
                        VideoPreflightExchange,
                        VideoPreflightExchange.request_id
                        == VideoStreamRequest.id,
                    )
                    .where(
                        ActiveVideoStream.device_credential_id
                        == device_credential_id,
                        VideoStreamRequest.state == "probing",
                        VideoStreamRequest.expires_at >= checked_at,
                        VideoPreflightExchange.device_answer_sdp == "",
                    )
                    .order_by(VideoPreflightExchange.created_at)
                    .limit(max(1, min(limit, 20)))
                )
            ).all()
        return tuple(
            self._video_preflight_exchange_record(request, stream, exchange)
            for request, stream, exchange in rows
        )

    async def record_video_preflight_answer(
        self,
        *,
        request_id: str,
        device_credential_id: str,
        device_answer_sdp: str,
        now: Optional[datetime] = None,
    ) -> VideoPreflightExchangeRecord:
        answered_at = now or utc_now()
        answer = normalize_video_preflight_answer(device_answer_sdp)
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(
                        VideoStreamRequest,
                        ActiveVideoStream,
                        VideoPreflightExchange,
                    )
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id
                        == VideoStreamRequest.active_stream_id,
                    )
                    .join(
                        VideoPreflightExchange,
                        VideoPreflightExchange.request_id
                        == VideoStreamRequest.id,
                    )
                    .where(VideoStreamRequest.id == request_id)
                )
            ).first()
            if row is None:
                raise ControlPlaneError("Video preflight offer not found.")
            request, stream, exchange = row
            if stream.device_credential_id != device_credential_id:
                raise ControlPlaneError(
                    "Video stream request belongs to another device."
                )
            if (
                request.state != "probing"
                or as_utc(request.expires_at) < answered_at
            ):
                raise ControlPlaneError(
                    "Video stream request is not awaiting a preflight answer."
                )
            exchange.device_answer_sdp = answer
            exchange.updated_at = answered_at
            await session.commit()
            return self._video_preflight_exchange_record(
                request,
                stream,
                exchange,
            )

    async def record_video_preflight_result(
        self,
        *,
        request_id: str,
        device_credential_id: str,
        route_kind: str,
        estimated_uplink_bps: int,
        now: Optional[datetime] = None,
    ) -> VideoStreamRequestRecord:
        measured_at = now or utc_now()
        clean_route = route_kind.strip().lower()
        if clean_route not in {"direct", "routed"}:
            raise ControlPlaneError("Preflight route must be Direct or Routed.")
        estimate = int(estimated_uplink_bps)
        if not 1 <= estimate <= 1_000_000_000:
            raise ControlPlaneError("Preflight bandwidth estimate is invalid.")
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(VideoStreamRequest, ActiveVideoStream)
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id
                        == VideoStreamRequest.active_stream_id,
                    )
                    .where(VideoStreamRequest.id == request_id)
                )
            ).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream = row
            if stream.device_credential_id != device_credential_id:
                raise ControlPlaneError(
                    "Video stream request belongs to another device."
                )
            if as_utc(request.expires_at) < measured_at:
                request.state = "expired"
                await session.commit()
                raise ControlPlaneError("Video stream request has expired.")
            if request.state not in {
                "pending",
                "probing",
                "awaiting_approval",
            }:
                raise ControlPlaneError(
                    "Video stream request is not awaiting preflight."
                )
            request.state = "awaiting_approval"
            request.route_kind = clean_route
            request.estimated_uplink_bps = estimate
            request.quality_source_width = stream.source_width
            request.quality_source_height = stream.source_height
            request.quality_source_fps_milli = stream.source_fps_milli
            await session.execute(
                delete(VideoPreflightExchange).where(
                    VideoPreflightExchange.request_id == request.id
                )
            )
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=request.organization_id,
                    actor_type="organization_device",
                    actor_id=device_credential_id,
                    event_type="video.preflight_completed",
                    details_json=json.dumps({"request_id": request.id}),
                    created_at=measured_at,
                )
            )
            await self._notify_video_stream_change(
                session, request.organization_id
            )
            await session.commit()
            return self._video_stream_request_record(request, stream)

    async def record_video_stream_decision(
        self,
        *,
        request_id: str,
        device_credential_id: str = "",
        requester_user_id: str = "",
        organization_id: str = "",
        decision: str,
        selected_width: int = 0,
        selected_height: int = 0,
        selected_fps: float = 0.0,
        selected_bitrate_bps: int = 0,
        now: Optional[datetime] = None,
    ) -> VideoStreamRequestRecord:
        decided_at = now or utc_now()
        clean_decision = decision.strip().lower()
        if clean_decision not in {"approve", "decline"}:
            raise ControlPlaneError("Video decision must be Approve or Decline.")
        width = max(0, min(int(selected_width), 16384))
        height = max(0, min(int(selected_height), 16384))
        fps_milli = max(0, min(int(round(selected_fps * 1000)), 240000))
        bitrate = max(0, min(int(selected_bitrate_bps), 1_000_000_000))
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(VideoStreamRequest, ActiveVideoStream)
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id == VideoStreamRequest.active_stream_id,
                    )
                    .where(VideoStreamRequest.id == request_id)
                )
            ).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream = row
            if requester_user_id:
                if (
                    request.requester_user_id != requester_user_id
                    or request.organization_id != organization_id
                    or not request.remote_control_enabled
                ):
                    raise ControlPlaneError(
                        "Remote video control is not enabled for this request."
                    )
            elif stream.device_credential_id != device_credential_id:
                raise ControlPlaneError(
                    "Video stream request belongs to another device."
                )
            if as_utc(request.expires_at) < decided_at:
                request.state = "expired"
                await session.commit()
                raise ControlPlaneError("Video stream request has expired.")
            if request.state != "awaiting_approval":
                raise ControlPlaneError(
                    "Video stream request is not awaiting pilot approval."
                )
            active_requests = tuple((await session.scalars(
                select(VideoStreamRequest)
                .join(
                    ActiveVideoStream,
                    ActiveVideoStream.id == VideoStreamRequest.active_stream_id,
                )
                .where(
                    ActiveVideoStream.device_credential_id
                    == stream.device_credential_id,
                    VideoStreamRequest.id != request.id,
                    VideoStreamRequest.state.in_(("approved", "streaming")),
                )
                .order_by(VideoStreamRequest.requested_at)
                .with_for_update()
            )).all())
            if clean_decision == "approve":
                if fps_milli <= 0 or bitrate <= 0:
                    raise ControlPlaneError(
                        "An approved stream requires a frame rate and bitrate."
                    )
                if requester_user_id:
                    allowed_choices = managed_video_quality_choices(
                        source_width=(
                            request.quality_source_width
                            or stream.source_width
                        ),
                        source_height=(
                            request.quality_source_height
                            or stream.source_height
                        ),
                        source_fps=(
                            request.quality_source_fps_milli
                            or stream.source_fps_milli
                        ) / 1000.0,
                        usable_uplink_bps=request.estimated_uplink_bps,
                    )
                    selected_choice = next((
                        choice
                        for choice in allowed_choices
                        if choice["width"] == width
                        and choice["height"] == height
                        and int(round(choice["fps"] * 1000)) == fps_milli
                        and choice["bitrateBps"] == bitrate
                    ), None)
                    if (
                        selected_choice is None
                        or selected_choice["capacity"] == "insufficient"
                    ):
                        raise ControlPlaneError(
                            "Select one of the bandwidth-qualified video choices."
                        )
                if (
                    bitrate > request.estimated_uplink_bps
                    and not is_emergency_video_fallback(
                        width=width,
                        height=height,
                        fps_milli=fps_milli,
                        bitrate_bps=bitrate,
                    )
                ):
                    raise ControlPlaneError(
                        "Selected bitrate exceeds the measured usable uplink; "
                        "only the 640 px, 5 fps, 0.2 Mbps emergency fallback "
                        "may override a failed-low preflight result."
                    )
                if request.remote_control_enabled and active_requests:
                    raise ControlPlaneError(
                        "This R2C app is already sharing video with another member."
                    )
                request.state = "approved"
                request.expires_at = decided_at + timedelta(
                    seconds=VIDEO_SESSION_AUTHORIZATION_SECONDS
                )
                request.selected_width = width
                request.selected_height = height
                request.selected_fps_milli = fps_milli
                request.selected_bitrate_bps = bitrate
                request.status_message = ""
                redirect_message = (
                    f"Stream redirected to {request.requester_email}"
                )
                for displaced in active_requests:
                    displaced.state = "redirected"
                    displaced.status_message = redirect_message
                    displaced.stopped_at = decided_at
                    session.add(ControlPlaneAuditEvent(
                        organization_id=displaced.organization_id,
                        actor_type=(
                            "organization_user"
                            if requester_user_id
                            else "organization_device"
                        ),
                        actor_id=requester_user_id or device_credential_id,
                        event_type="video.redirected",
                        details_json=json.dumps({
                            "request_id": displaced.id,
                            "replacement_request_id": request.id,
                        }),
                        created_at=decided_at,
                    ))
            else:
                request.state = "declined"
                request.selected_width = 0
                request.selected_height = 0
                request.selected_fps_milli = 0
                request.selected_bitrate_bps = 0
                request.status_message = (
                    "App already streaming to "
                    f"{active_requests[0].requester_email}"
                    if active_requests
                    else "insufficient bandwidth"
                )
            request.decided_at = decided_at
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=request.organization_id,
                    actor_type=(
                        "organization_user"
                        if requester_user_id
                        else "organization_device"
                    ),
                    actor_id=requester_user_id or device_credential_id,
                    event_type=f"video.{request.state}",
                    details_json=json.dumps({"request_id": request.id}),
                    created_at=decided_at,
                )
            )
            await self._notify_video_stream_change(
                session, request.organization_id
            )
            await session.commit()
            return self._video_stream_request_record(request, stream)

    async def record_video_stream_unavailable(
        self,
        *,
        request_id: str,
        device_credential_id: str,
        stream_session_id: str = "",
        error_code: str = "e_nosuch_stream",
        now: Optional[datetime] = None,
    ) -> VideoStreamRequestRecord:
        """Close a request that no longer maps to a live tablet source."""
        reported_at = now or utc_now()
        clean_error = error_code.strip().lower()
        if clean_error != "e_nosuch_stream":
            raise ControlPlaneError("Unsupported video stream error code.")
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(VideoStreamRequest, ActiveVideoStream)
                    .join(
                        ActiveVideoStream,
                        ActiveVideoStream.id
                        == VideoStreamRequest.active_stream_id,
                    )
                    .where(VideoStreamRequest.id == request_id)
                )
            ).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream = row
            if stream.device_credential_id != device_credential_id:
                raise ControlPlaneError(
                    "Video stream request belongs to another device."
                )
            clean_session_id = stream_session_id.strip()
            if clean_session_id and stream.session_id != clean_session_id:
                raise ControlPlaneError(
                    "Video stream request identifies another stream session."
                )
            if request.state == clean_error:
                return self._video_stream_request_record(request, stream)
            if request.state not in {
                "pending",
                "probing",
                "awaiting_approval",
                "approved",
                "streaming",
            }:
                raise ControlPlaneError(
                    "Video stream request is no longer active."
                )
            request.state = clean_error
            request.stopped_at = reported_at
            await session.execute(
                delete(VideoPreflightExchange).where(
                    VideoPreflightExchange.request_id == request.id
                )
            )
            await session.execute(
                delete(VideoMediaExchange).where(
                    VideoMediaExchange.request_id == request.id
                )
            )
            session.add(
                ControlPlaneAuditEvent(
                    organization_id=request.organization_id,
                    actor_type="organization_device",
                    actor_id=device_credential_id,
                    event_type="video.e_nosuch_stream",
                    details_json=json.dumps({"request_id": request.id}),
                    created_at=reported_at,
                )
            )
            await self._notify_video_stream_change(
                session, request.organization_id
            )
            await session.commit()
            return self._video_stream_request_record(request, stream)

    async def start_video_media(
        self,
        *,
        request_id: str,
        organization_id: str,
        requester_user_id: str,
        browser_offer_sdp: str,
        relay_candidate_ms: int = 0,
        now: Optional[datetime] = None,
    ) -> VideoMediaExchangeRecord:
        started_at = now or utc_now()
        offer = normalize_session_description(browser_offer_sdp)
        async with self.sessions() as session:
            row = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream)
                .join(ActiveVideoStream, ActiveVideoStream.id == VideoStreamRequest.active_stream_id)
                .where(VideoStreamRequest.id == request_id)
            )).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream = row
            if request.organization_id != organization_id or request.requester_user_id != requester_user_id:
                raise ControlPlaneError("Video stream request belongs to another user.")
            if as_utc(request.expires_at) < started_at:
                request.state = "expired"
                await session.commit()
                raise ControlPlaneError("Video stream request has expired.")
            if request.state not in {"approved", "streaming"}:
                raise ControlPlaneError("Pilot approval is required before media signaling.")
            exchange = await session.get(VideoMediaExchange, request.id)
            if exchange is None:
                exchange = VideoMediaExchange(
                    request_id=request.id,
                    browser_offer_sdp=offer,
                    device_answer_sdp="",
                    created_at=started_at,
                    updated_at=started_at,
                    expires_at=request.expires_at,
                )
                session.add(exchange)
            else:
                exchange.browser_offer_sdp = offer
                exchange.device_answer_sdp = ""
                exchange.updated_at = started_at
            session.add(ControlPlaneAuditEvent(
                organization_id=request.organization_id,
                actor_type="organization_user",
                actor_id=requester_user_id,
                event_type="video.media_signaling_started",
                details_json=json.dumps({
                    "request_id": request.id,
                    "browser_relay_candidate_ms": max(
                        0, min(int(relay_candidate_ms), 60_000)
                    ),
                }),
                created_at=started_at,
            ))
            if self.engine.dialect.name == "postgresql":
                await session.execute(select(func.pg_notify("r2c_video_media", request.id)))
            await session.commit()
            return self._video_media_exchange_record(request, stream, exchange)

    async def get_video_media_exchange_for_requester(
        self, *, request_id: str, organization_id: str, requester_user_id: str
    ) -> VideoMediaExchangeRecord:
        async with self.sessions() as session:
            row = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream, VideoMediaExchange)
                .join(ActiveVideoStream, ActiveVideoStream.id == VideoStreamRequest.active_stream_id)
                .join(VideoMediaExchange, VideoMediaExchange.request_id == VideoStreamRequest.id)
                .where(VideoStreamRequest.id == request_id)
            )).first()
        if row is None:
            raise ControlPlaneError("Video media signaling was not found.")
        request, stream, exchange = row
        if request.organization_id != organization_id or request.requester_user_id != requester_user_id:
            raise ControlPlaneError("Video stream request belongs to another user.")
        return self._video_media_exchange_record(request, stream, exchange)

    async def get_pending_video_media_offer(
        self, *, request_id: str, now: Optional[datetime] = None
    ) -> Optional[VideoMediaExchangeRecord]:
        checked_at = now or utc_now()
        async with self.sessions() as session:
            row = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream, VideoMediaExchange)
                .join(ActiveVideoStream, ActiveVideoStream.id == VideoStreamRequest.active_stream_id)
                .join(VideoMediaExchange, VideoMediaExchange.request_id == VideoStreamRequest.id)
                .where(
                    VideoStreamRequest.id == request_id,
                    VideoStreamRequest.state.in_(("approved", "streaming")),
                    VideoStreamRequest.expires_at >= checked_at,
                    VideoMediaExchange.device_answer_sdp == "",
                )
            )).first()
        if row is None:
            return None
        return self._video_media_exchange_record(*row)

    async def list_pending_video_media_offers_for_device(
        self, *, device_credential_id: str, now: Optional[datetime] = None
    ) -> tuple[VideoMediaExchangeRecord, ...]:
        checked_at = now or utc_now()
        async with self.sessions() as session:
            rows = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream, VideoMediaExchange)
                .join(ActiveVideoStream, ActiveVideoStream.id == VideoStreamRequest.active_stream_id)
                .join(VideoMediaExchange, VideoMediaExchange.request_id == VideoStreamRequest.id)
                .where(
                    ActiveVideoStream.device_credential_id == device_credential_id,
                    VideoStreamRequest.state.in_(("approved", "streaming")),
                    VideoStreamRequest.expires_at >= checked_at,
                    VideoMediaExchange.device_answer_sdp == "",
                )
            )).all()
        return tuple(self._video_media_exchange_record(*row) for row in rows)

    async def record_video_media_answer(
        self, *, request_id: str, device_credential_id: str, device_answer_sdp: str,
        now: Optional[datetime] = None,
    ) -> VideoMediaExchangeRecord:
        answered_at = now or utc_now()
        answer = normalize_session_description(device_answer_sdp)
        async with self.sessions() as session:
            row = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream, VideoMediaExchange)
                .join(ActiveVideoStream, ActiveVideoStream.id == VideoStreamRequest.active_stream_id)
                .join(VideoMediaExchange, VideoMediaExchange.request_id == VideoStreamRequest.id)
                .where(VideoStreamRequest.id == request_id)
            )).first()
            if row is None:
                raise ControlPlaneError("Video media offer not found.")
            request, stream, exchange = row
            if stream.device_credential_id != device_credential_id:
                raise ControlPlaneError("Video stream request belongs to another device.")
            if as_utc(request.expires_at) < answered_at:
                request.state = "expired"
                request.status_message = "Video request timed out."
                request.stopped_at = answered_at
                await session.delete(exchange)
                await session.commit()
                raise ControlPlaneError("Video stream request has expired.")
            if request.state not in {"approved", "streaming"}:
                raise ControlPlaneError("Video stream is not approved.")
            exchange.device_answer_sdp = answer
            exchange.updated_at = answered_at
            await session.commit()
            return self._video_media_exchange_record(request, stream, exchange)

    async def mark_video_streaming(
        self, *, request_id: str, organization_id: str, requester_user_id: str,
        now: Optional[datetime] = None,
    ) -> VideoStreamRequestRecord:
        started_at = now or utc_now()
        async with self.sessions() as session:
            row = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream)
                .join(ActiveVideoStream, ActiveVideoStream.id == VideoStreamRequest.active_stream_id)
                .where(VideoStreamRequest.id == request_id)
            )).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream = row
            if request.organization_id != organization_id or request.requester_user_id != requester_user_id:
                raise ControlPlaneError("Video stream request belongs to another user.")
            if as_utc(request.expires_at) < started_at:
                request.state = "expired"
                request.status_message = "Video request timed out."
                request.stopped_at = started_at
                exchange = await session.get(VideoMediaExchange, request.id)
                if exchange is not None:
                    await session.delete(exchange)
                await session.commit()
                raise ControlPlaneError("Video stream request has expired.")
            if request.state not in {"approved", "streaming"}:
                raise ControlPlaneError("Video stream is not approved.")
            request.state = "streaming"
            if request.started_at is None:
                request.started_at = started_at
            session.add(ControlPlaneAuditEvent(
                organization_id=request.organization_id,
                actor_type="organization_user",
                actor_id=requester_user_id,
                event_type="video.streaming",
                details_json=json.dumps({"request_id": request.id}),
                created_at=started_at,
            ))
            await self._notify_video_stream_change(session, request.organization_id)
            await session.commit()
            return self._video_stream_request_record(request, stream)

    async def record_video_media_metrics(
        self,
        *,
        request_id: str,
        organization_id: str,
        requester_user_id: str,
        metrics_session_id: str,
        audio_bytes_sent: int,
        audio_bytes_received: int,
        video_bytes_received: int,
        now: Optional[datetime] = None,
    ) -> VideoStreamRequestRecord:
        measured_at = now or utc_now()
        clean_session_id = metrics_session_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", clean_session_id):
            raise ControlPlaneError("Video media metrics session is invalid.")
        counters = tuple(
            max(0, int(value))
            for value in (audio_bytes_sent, audio_bytes_received, video_bytes_received)
        )
        relay_delta = 0
        result_record = None
        async with self.sessions() as session:
            row = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream)
                .join(ActiveVideoStream, ActiveVideoStream.id == VideoStreamRequest.active_stream_id)
                .where(VideoStreamRequest.id == request_id)
                .with_for_update()
            )).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream = row
            if request.organization_id != organization_id or request.requester_user_id != requester_user_id:
                raise ControlPlaneError("Video stream request belongs to another user.")
            if request.state not in {"approved", "streaming", "stopped"}:
                raise ControlPlaneError("Video media metrics cannot be recorded.")
            segment = await session.get(
                VideoMediaMetricsSegment,
                {"request_id": request.id, "metrics_session_id": clean_session_id},
            )
            if segment is None:
                segment = VideoMediaMetricsSegment(
                    request_id=request.id,
                    metrics_session_id=clean_session_id,
                    audio_bytes_sent=0,
                    audio_bytes_received=0,
                    video_bytes_received=0,
                )
                session.add(segment)
            previous = (
                segment.audio_bytes_sent,
                segment.audio_bytes_received,
                segment.video_bytes_received,
            )
            deltas = tuple(max(0, current - prior) for current, prior in zip(counters, previous))
            relay_delta = sum(deltas)
            request.audio_bytes_sent += deltas[0]
            request.audio_bytes_received += deltas[1]
            request.video_bytes_received += deltas[2]
            segment.audio_bytes_sent = max(previous[0], counters[0])
            segment.audio_bytes_received = max(previous[1], counters[1])
            segment.video_bytes_received = max(previous[2], counters[2])
            segment.updated_at = measured_at
            await session.commit()
            result_record = self._video_stream_request_record(request, stream)
        if relay_delta:
            # The active browser media path currently requires TURN. Attribute
            # the measured RTP payload to the organization; provider analytics
            # can later reconcile protocol overhead and free-tier adjustments.
            await self.increment_daily_usage(
                organization_id=organization_id,
                turn_relay_bytes=relay_delta,
                now=measured_at,
            )
        return result_record

    async def stop_video_stream(
        self, *, request_id: str, organization_id: str, requester_user_id: str,
        reason: str = "",
        now: Optional[datetime] = None,
    ) -> VideoStreamRequestRecord:
        stopped_at = now or utc_now()
        async with self.sessions() as session:
            row = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream)
                .join(ActiveVideoStream, ActiveVideoStream.id == VideoStreamRequest.active_stream_id)
                .where(VideoStreamRequest.id == request_id)
            )).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream = row
            if request.organization_id != organization_id or request.requester_user_id != requester_user_id:
                raise ControlPlaneError("Video stream request belongs to another user.")
            if request.state not in {"approved", "streaming", "stopped"}:
                raise ControlPlaneError("Video stream cannot be stopped.")
            if request.state == "stopped":
                return self._video_stream_request_record(request, stream)
            request.state = "stopped"
            clean_reason = str(reason or "").strip()[:400]
            request.status_message = clean_reason
            request.stopped_at = stopped_at
            exchange = await session.get(VideoMediaExchange, request.id)
            if exchange is not None:
                await session.delete(exchange)
            session.add(ControlPlaneAuditEvent(
                organization_id=request.organization_id,
                actor_type="organization_user",
                actor_id=requester_user_id,
                event_type="video.stopped",
                details_json=json.dumps({
                    "request_id": request.id,
                    "reason": clean_reason,
                }),
                created_at=stopped_at,
            ))
            await self._notify_video_stream_change(session, request.organization_id)
            await session.commit()
            return self._video_stream_request_record(request, stream)

    async def stop_video_stream_from_device(
        self, *, request_id: str, device_credential_id: str,
        reason: str = "device_terminated", now: Optional[datetime] = None,
    ) -> VideoStreamRequestRecord:
        stopped_at = now or utc_now()
        async with self.sessions() as session:
            row = (await session.execute(
                select(VideoStreamRequest, ActiveVideoStream)
                .join(ActiveVideoStream, ActiveVideoStream.id == VideoStreamRequest.active_stream_id)
                .where(VideoStreamRequest.id == request_id)
            )).first()
            if row is None:
                raise ControlPlaneError("Video stream request not found.")
            request, stream = row
            if stream.device_credential_id != device_credential_id:
                raise ControlPlaneError("Video stream request belongs to another device.")
            if request.state not in {
                "approved", "streaming", "stopped", "redirected"
            }:
                raise ControlPlaneError("Video stream cannot be stopped.")
            if request.state not in {"stopped", "redirected"}:
                clean_reason = str(reason or "device_terminated").strip()[:400]
                redirected = clean_reason.startswith("Stream redirected to ")
                request.state = "redirected" if redirected else "stopped"
                # Preserve the device's bounded terminal reason after the SDP
                # exchange is deleted so the requesting browser can explain
                # an attach/source failure instead of showing a generic status
                # error.
                request.status_message = clean_reason
                request.stopped_at = stopped_at
                if not redirected:
                    exchange = await session.get(VideoMediaExchange, request.id)
                    if exchange is not None:
                        await session.delete(exchange)
                session.add(ControlPlaneAuditEvent(
                    organization_id=request.organization_id,
                    actor_type="organization_device",
                    actor_id=device_credential_id,
                    event_type=(
                        "video.redirected"
                        if redirected
                        else "video.stopped_by_device"
                    ),
                    details_json=json.dumps({
                        "request_id": request.id,
                        "reason": clean_reason[:80],
                    }),
                    created_at=stopped_at,
                ))
                await self._notify_video_stream_change(session, request.organization_id)
                await session.commit()
            return self._video_stream_request_record(request, stream)

    @staticmethod
    def _video_preflight_exchange_record(
        request: VideoStreamRequest,
        stream: ActiveVideoStream,
        exchange: VideoPreflightExchange,
    ) -> VideoPreflightExchangeRecord:
        source_width = request.quality_source_width or stream.source_width
        source_height = request.quality_source_height or stream.source_height
        source_fps_milli = (
            request.quality_source_fps_milli or stream.source_fps_milli
        )
        return VideoPreflightExchangeRecord(
            request_id=request.id,
            organization_id=request.organization_id,
            device_credential_id=stream.device_credential_id,
            requester_user_id=request.requester_user_id,
            state=request.state,
            status_message=request.status_message or "",
            route_kind=request.route_kind,
            estimated_uplink_bps=request.estimated_uplink_bps,
            remote_control_enabled=bool(request.remote_control_enabled),
            source_width=source_width,
            source_height=source_height,
            source_fps=source_fps_milli / 1000.0,
            browser_offer_sdp=exchange.browser_offer_sdp,
            device_answer_sdp=exchange.device_answer_sdp,
            expires_at=as_utc(request.expires_at),
        )

    @staticmethod
    def _video_media_exchange_record(
        request: VideoStreamRequest,
        stream: ActiveVideoStream,
        exchange: VideoMediaExchange,
    ) -> VideoMediaExchangeRecord:
        return VideoMediaExchangeRecord(
            request_id=request.id,
            organization_id=request.organization_id,
            device_credential_id=stream.device_credential_id,
            requester_user_id=request.requester_user_id,
            stream_session_id=stream.session_id,
            requester_email=request.requester_email,
            route_kind=request.route_kind,
            selected_width=request.selected_width,
            selected_height=request.selected_height,
            selected_fps=request.selected_fps_milli / 1000.0,
            selected_bitrate_bps=request.selected_bitrate_bps,
            state=request.state,
            status_message=request.status_message or "",
            browser_offer_sdp=exchange.browser_offer_sdp,
            device_answer_sdp=exchange.device_answer_sdp,
            expires_at=as_utc(request.expires_at),
        )

    @staticmethod
    def _active_video_stream_record(
        stream: ActiveVideoStream,
    ) -> ActiveVideoStreamRecord:
        return ActiveVideoStreamRecord(
            id=stream.id,
            session_id=stream.session_id,
            organization_id=stream.organization_id,
            device_credential_id=stream.device_credential_id,
            device_name=stream.device_name or "Unknown device",
            incident_name=stream.incident_name,
            drone_designator=stream.drone_designator,
            source_width=stream.source_width,
            source_height=stream.source_height,
            source_fps=stream.source_fps_milli / 1000.0,
            source_bitrate_bps=stream.source_bitrate_bps,
            source_codec=stream.source_codec,
            media_kind=stream.media_kind,
            recorded_at=as_utc(stream.recorded_at),
            duration_ms=max(0, stream.duration_ms),
            thumbnail_revision=stream.thumbnail_revision,
            timezone_name=stream.timezone_name or "UTC",
            remote_control_enabled=bool(stream.remote_control_enabled),
            last_seen_at=as_utc(stream.last_seen_at),
            expires_at=as_utc(stream.expires_at),
        )

    @staticmethod
    def _recording_download_request_record(
        item: RecordingDownloadRequest,
        stream: ActiveVideoStream,
    ) -> RecordingDownloadRequestRecord:
        return RecordingDownloadRequestRecord(
            id=item.id,
            organization_id=item.organization_id,
            device_credential_id=item.device_credential_id,
            device_name=stream.device_name or "Unknown device",
            stream_session_id=stream.session_id,
            drone_designator=stream.drone_designator,
            requester_user_id=item.requester_user_id,
            requester_email=item.requester_email,
            remote_control_enabled=bool(item.remote_control_enabled),
            state=item.state,
            status_message=item.status_message or "",
            filename=item.filename or "",
            media_type=item.media_type or "video/mp4",
            byte_count=max(0, item.byte_count),
            sha256=item.sha256 or "",
            storage_relpath=item.storage_relpath or "",
            requested_at=as_utc(item.requested_at),
            expires_at=as_utc(item.expires_at),
            completed_at=as_utc(item.completed_at),
        )

    @staticmethod
    def _video_stream_request_record(
        request: VideoStreamRequest,
        stream: ActiveVideoStream,
    ) -> VideoStreamRequestRecord:
        return VideoStreamRequestRecord(
            id=request.id,
            organization_id=request.organization_id,
            device_credential_id=stream.device_credential_id,
            device_name=stream.device_name or "Unknown device",
            stream_session_id=stream.session_id,
            incident_name=stream.incident_name,
            drone_designator=stream.drone_designator,
            requester_user_id=request.requester_user_id,
            requester_email=request.requester_email,
            source_width=stream.source_width,
            source_height=stream.source_height,
            source_fps=stream.source_fps_milli / 1000.0,
            source_bitrate_bps=stream.source_bitrate_bps,
            source_codec=stream.source_codec,
            timezone_name=stream.timezone_name or "UTC",
            remote_control_enabled=bool(request.remote_control_enabled),
            state=request.state,
            status_message=request.status_message or "",
            route_kind=request.route_kind,
            estimated_uplink_bps=request.estimated_uplink_bps,
            selected_width=request.selected_width,
            selected_height=request.selected_height,
            selected_fps=request.selected_fps_milli / 1000.0,
            selected_bitrate_bps=request.selected_bitrate_bps,
            requested_at=as_utc(request.requested_at),
            expires_at=as_utc(request.expires_at),
            started_at=as_utc(request.started_at) if request.started_at else None,
            stopped_at=as_utc(request.stopped_at) if request.stopped_at else None,
            audio_bytes_sent=request.audio_bytes_sent,
            audio_bytes_received=request.audio_bytes_received,
            video_bytes_received=request.video_bytes_received,
        )

    @staticmethod
    def _campaign_record(
        campaign: EnrollmentCampaign,
        checked_at: Optional[datetime] = None,
    ) -> EnrollmentCampaignRecord:
        expires_at = as_utc(campaign.expires_at)
        state = campaign.state
        if checked_at is not None and state == "active" and expires_at < checked_at:
            state = "expired"
        return EnrollmentCampaignRecord(
            id=campaign.id,
            organization_id=campaign.organization_id,
            label=campaign.label,
            state=state,
            max_redemptions=campaign.max_redemptions,
            redemption_count=campaign.redemption_count,
            expires_at=expires_at,
            created_at=as_utc(campaign.created_at),
            revoked_at=as_utc(campaign.revoked_at),
            token_generation=campaign.token_generation or "",
        )

    @staticmethod
    def _device_credential_admin_record(
        credential: DeviceCredential,
        checked_at: datetime,
    ) -> DeviceCredentialAdminRecord:
        expires_at = as_utc(credential.expires_at)
        state = credential.state
        if state == "active" and expires_at < checked_at:
            state = "expired"
        return DeviceCredentialAdminRecord(
            id=credential.id,
            organization_id=credential.organization_id,
            device_name=credential.device_name,
            platform=credential.platform,
            authorized_user_id=credential.authorized_user_id,
            functionality_release=credential.functionality_release,
            state=state,
            created_at=as_utc(credential.created_at),
            expires_at=expires_at,
            last_used_at=as_utc(credential.last_used_at),
            reauth_requested_at=as_utc(credential.reauth_requested_at),
        )

    @staticmethod
    def _ledger_record(entry: BillingLedgerEntry) -> BillingLedgerRecord:
        return BillingLedgerRecord(
            id=entry.id,
            entry_type=entry.entry_type,
            amount=Decimal(entry.amount),
            currency=entry.currency,
            description=entry.description,
            external_reference=entry.external_reference,
            created_at=as_utc(entry.created_at),
        )
