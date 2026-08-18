import os
import io
import re
import sys
import math
import csv
import json
import tarfile
import subprocess
import requests
import asyncio
import asyncpg
import base64
import warnings
import traceback
import logging
import secrets
import hashlib
import anyio
from calendar import monthrange
from dataclasses import replace
from decimal import Decimal
import numpy as np
from urllib.parse import quote, urlencode
from pprint import pprint
from datetime import datetime, date, timedelta, timezone, UTC
from zoneinfo import ZoneInfo
from timezonefinder import TimezoneFinder
from typing import Optional, Annotated, Literal
from contextlib import asynccontextmanager

from fastapi import Security, Depends, FastAPI, Request, HTTPException, Query, Form, Header
from fastapi import status, Response, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi import BackgroundTasks
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from starlette.status import HTTP_403_FORBIDDEN
from starlette.middleware.sessions import SessionMiddleware

from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, MetaData, Table, delete, select, text
from sqlalchemy import Column, Integer, BigInteger, String, Float, Boolean, DateTime, desc, func, or_, and_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker, Session
from suncalc import get_times
from google.cloud.sql.connector import Connector, IPTypes
from faa_proxy import FaaNotamProxy, FaaProxyError
from control_plane import (
    AUDIT_EVENT_CATEGORY_PREFIXES,
    AUDIT_EVENT_EXPORT_LIMIT,
    AUDIT_EVENT_HOT_DAYS,
    AUDIT_EVENT_PAGE_SIZE,
    AUDIT_EVENT_RECENT_DAYS,
    AUDIT_EVENT_RECENT_LIMIT,
    AUDIT_EVENT_RETENTION_DAYS,
    ControlPlaneError,
    ControlPlaneStore,
    DeviceCredentialRecord,
    DuplicateOrganizationError,
    InvalidOrganizationError,
    ROLE_DESCRIPTIONS,
    managed_video_quality_choices,
    normalize_video_preflight_answer,
    require_separate_database,
    stream_link_code,
    recording_link_code,
    tablet_link_code,
)
from enrollment import (
    ControlPlaneTokenService,
    EnrollmentTokenError,
    public_device_configuration,
)
from platform_admin import (
    AggregateUsage,
    BigQueryBillingSnapshotProvider,
    CostBreakdown,
    OrganizationBillingSummary,
    allocate_platform_costs,
    build_illustrative_platform_snapshot,
    build_pending_platform_snapshot,
    public_snapshot_dict,
)
from platform_admin_identity import (
    PlatformAdminIdentityError,
    SecretManagerPlatformAdminIdentityProvider,
)
from platform_admin_auth import (
    GmailApiPlatformAdminEmailSender,
    GoogleOidcClient,
    MicrosoftOidcClient,
    PlatformAdminAuthError,
    SmtpPlatformAdminEmailSender,
)
from turn_credentials import (
    CloudflareTurnCredentialProvider,
    sanitize_ice_servers,
)
from app_store_connect_webhook import (
    AppStoreConnectSignatureError,
    AppStoreConnectWebhookError,
    authenticate_and_parse as authenticate_app_store_connect_webhook,
)

# --- CONFIGURATION & DATABASE SETUP ---
DB_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./test.db") # Defaults to local file if no Cloud SQL
DB_USER = os.environ.get("DB_USER", "undefined")
DB_PASS = os.environ.get("DB_PASS", "undefined")
DB_NAME = os.environ.get("DB_NAME", "undefined")
API_KEY_NAME = "X-SAR-Token"
DEPLOYMENT_GATE_KEY = os.environ.get("DEPLOYMENT_GATE_KEY", "").strip()
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
TRACKER_ADMIN_USER = os.environ.get("TRACKER_ADMIN_USER", "admin")
TRACKER_ADMIN_PASS = os.environ.get("TRACKER_ADMIN_PASS", "")
LEGACY_ADMIN_ENABLED = os.environ.get(
    "LEGACY_ADMIN_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
PLATFORM_BILLING_SOURCE = os.environ.get(
    "PLATFORM_BILLING_SOURCE", "illustrative"
).strip().lower()
PLATFORM_BILLING_PROJECT = os.environ.get(
    "PLATFORM_BILLING_PROJECT", "r2c-tracker-platform"
).strip()
PLATFORM_BILLING_DATASET = os.environ.get(
    "PLATFORM_BILLING_DATASET", "r2c_billing_export"
).strip()
PLATFORM_BILLING_INCLUDED_PROJECTS = tuple(
    project_id.strip()
    for project_id in os.environ.get(
        "PLATFORM_BILLING_INCLUDED_PROJECTS", ""
    ).split(",")
    if project_id.strip()
)
CONTROL_PLANE_DATABASE_URL = os.environ.get(
    "CONTROL_PLANE_DATABASE_URL", ""
).strip()
CONTROL_PLANE_SIMULATION = (
    os.environ.get("CONTROL_PLANE_MODE", "simulation").strip().lower()
    != "live"
)
RELEASE_STAGING_MODE = (
    os.environ.get("RELEASE_STAGING_MODE", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)
CONTROL_PLANE_SIGNING_KEY = os.environ.get(
    "CONTROL_PLANE_SIGNING_KEY", ""
).strip()
MANAGED_REQUEST_INGEST_KEY = os.environ.get(
    "MANAGED_REQUEST_INGEST_KEY", ""
).strip()
CONTROL_PLANE_PUBLIC_URL = os.environ.get(
    "CONTROL_PLANE_PUBLIC_URL", "https://r2c-tracker.com"
).strip()
CORS_ALLOWED_ORIGINS = tuple(
    origin.strip().rstrip("/")
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        CONTROL_PLANE_PUBLIC_URL,
    ).split(",")
    if origin.strip()
)
SESSION_COOKIE_HTTPS_ONLY = (
    os.environ.get("SESSION_COOKIE_HTTPS_ONLY", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)
DEVICE_CREDENTIAL_ISSUANCE_ENABLED = (
    os.environ.get("DEVICE_CREDENTIAL_ISSUANCE_ENABLED", "false")
    .strip()
    .lower()
    in {"1", "true", "yes", "on"}
)
CONTROL_PLANE_TRACKER_BASE_URL = os.environ.get(
    "CONTROL_PLANE_TRACKER_BASE_URL",
    CONTROL_PLANE_PUBLIC_URL,
).strip().rstrip("/")
VIDEO_ICE_SERVERS_JSON = os.environ.get(
    "VIDEO_ICE_SERVERS_JSON",
    "[]",
).strip()
CLOUDFLARE_TURN_KEY_ID = os.environ.get(
    "CLOUDFLARE_TURN_KEY_ID",
    "",
).strip()
CLOUDFLARE_TURN_API_TOKEN = os.environ.get(
    "CLOUDFLARE_TURN_API_TOKEN",
    "",
).strip()
CLOUDFLARE_TURN_CREDENTIAL_TTL_SECONDS = int(
    os.environ.get("CLOUDFLARE_TURN_CREDENTIAL_TTL_SECONDS", "3600")
)
MAX_ARCHIVE_UPLOAD_BYTES = int(
    os.environ.get("MAX_ARCHIVE_UPLOAD_BYTES", str(100 * 1024 * 1024))
)
MAX_ARCHIVE_MEMBERS = int(os.environ.get("MAX_ARCHIVE_MEMBERS", "10000"))
MAX_FLIGHT_LOG_MEMBERS = int(os.environ.get("MAX_FLIGHT_LOG_MEMBERS", "5000"))
MAX_FLIGHT_LOG_BYTES = int(
    os.environ.get("MAX_FLIGHT_LOG_BYTES", str(16 * 1024 * 1024))
)
MAX_ARCHIVE_EXPANDED_BYTES = int(
    os.environ.get("MAX_ARCHIVE_EXPANDED_BYTES", str(512 * 1024 * 1024))
)


async def read_upload_with_limit(
    upload: UploadFile,
    max_bytes: int = MAX_ARCHIVE_UPLOAD_BYTES,
) -> bytes:
    """Read an upload without allowing an unbounded in-memory allocation."""
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(min(1024 * 1024, max_bytes - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(
                f"Archive exceeds the {max_bytes // (1024 * 1024)} MiB upload limit."
            )
        chunks.append(chunk)


def reviewed_flight_archive_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Select bounded JSON flight logs and reject archive resource exhaustion."""
    selected = []
    expanded_bytes = 0
    for member_count, member in enumerate(tar, start=1):
        if member_count > MAX_ARCHIVE_MEMBERS:
            raise ValueError(
                f"Archive contains more than {MAX_ARCHIVE_MEMBERS} entries."
            )
        if not (
            member.isfile()
            and member.name.endswith(".json")
            and "flightlog_" in os.path.basename(member.name)
        ):
            continue
        if member.size < 0 or member.size > MAX_FLIGHT_LOG_BYTES:
            raise ValueError(
                f"{member.name} exceeds the per-flight-log size limit."
            )
        selected.append(member)
        if len(selected) > MAX_FLIGHT_LOG_MEMBERS:
            raise ValueError(
                f"Archive contains more than {MAX_FLIGHT_LOG_MEMBERS} flight logs."
            )
        expanded_bytes += member.size
        if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ValueError("Archive expands beyond the permitted flight-log limit.")
    return sorted(selected, key=lambda member: member.name)


class DeviceEnrollmentRedeemRequest(BaseModel):
    token: str = Field(min_length=24, max_length=4096)
    device_name: str = Field(min_length=1, max_length=160)
    platform: Literal["android", "ios"]
    functionality_release: int = Field(default=0, ge=0, le=1_000_000)


class BrowserVideoPreflightOffer(BaseModel):
    sdp: str = Field(min_length=3, max_length=262_144)
    form_token: str = Field(min_length=16, max_length=512)
    relay_candidate_ms: int = Field(default=0, ge=0, le=60_000)


class BrowserVideoMediaOffer(BaseModel):
    sdp: str = Field(min_length=3, max_length=262_144)
    form_token: str = Field(min_length=16, max_length=512)
    relay_candidate_ms: int = Field(default=0, ge=0, le=60_000)


class BrowserVideoQualitySelection(BaseModel):
    form_token: str = Field(min_length=16, max_length=512)
    width: int = Field(ge=2, le=16_384)
    height: int = Field(ge=2, le=16_384)
    fps: float = Field(gt=0, le=240)
    bitrate_bps: int = Field(gt=0, le=1_000_000_000)


class BrowserVideoMediaState(BaseModel):
    form_token: str = Field(min_length=16, max_length=512)
    reason: str = Field(default="", max_length=400)


class BrowserVideoMediaMetrics(BrowserVideoMediaState):
    metrics_session_id: str = Field(min_length=8, max_length=64)
    audio_bytes_sent: int = Field(ge=0, le=10_000_000_000_000)
    audio_bytes_received: int = Field(ge=0, le=10_000_000_000_000)
    video_bytes_received: int = Field(ge=0, le=10_000_000_000_000)
    diagnostic_event: str = Field(default="sample", max_length=64)
    diagnostic_detail: str = Field(default="", max_length=400)
    peer_connection_state: str = Field(default="", max_length=32)
    ice_connection_state: str = Field(default="", max_length=32)
    ice_gathering_state: str = Field(default="", max_length=32)
    signaling_state: str = Field(default="", max_length=32)
    video_track_state: str = Field(default="", max_length=32)
    video_element_ready_state: int = Field(default=0, ge=0, le=4)
    video_element_paused: bool = True
    video_element_width: int = Field(default=0, ge=0, le=16_384)
    video_element_height: int = Field(default=0, ge=0, le=16_384)
    video_packets_received: int = Field(default=0, ge=0, le=10_000_000_000_000)
    video_frames_received: int = Field(default=0, ge=0, le=10_000_000_000_000)
    video_frames_decoded: int = Field(default=0, ge=0, le=10_000_000_000_000)
    video_frames_presented: int = Field(default=0, ge=0, le=10_000_000_000_000)
    video_frames_dropped: int = Field(default=0, ge=0, le=10_000_000_000_000)
    video_key_frames_decoded: int = Field(default=0, ge=0, le=10_000_000_000_000)
    video_codec: str = Field(default="", max_length=120)
    decoder_implementation: str = Field(default="", max_length=120)


def optional_iso_datetime(value: object) -> Optional[datetime]:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControlPlaneError("Recorded video time is invalid.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def public_video_ice_servers() -> list[dict[str, object]]:
    try:
        configured = json.loads(VIDEO_ICE_SERVERS_JSON)
    except json.JSONDecodeError:
        logging.error("VIDEO_ICE_SERVERS_JSON is not valid JSON")
        return []
    return sanitize_ice_servers(configured)


video_ice_server_provider = CloudflareTurnCredentialProvider(
    key_id=CLOUDFLARE_TURN_KEY_ID,
    api_token=CLOUDFLARE_TURN_API_TOKEN,
    fallback_ice_servers=public_video_ice_servers(),
    credential_ttl_seconds=CLOUDFLARE_TURN_CREDENTIAL_TTL_SECONDS,
)
require_separate_database(CONTROL_PLANE_DATABASE_URL, DB_URL)
control_plane_store = (
    ControlPlaneStore(CONTROL_PLANE_DATABASE_URL)
    if CONTROL_PLANE_DATABASE_URL
    else None
)
platform_admin_identity_provider = (
    SecretManagerPlatformAdminIdentityProvider()
    if control_plane_store is not None
    else None
)
google_oidc_client = GoogleOidcClient.from_environment()
microsoft_oidc_client = MicrosoftOidcClient.from_environment()
gmail_email_sender = GmailApiPlatformAdminEmailSender.from_environment()
smtp_email_sender = SmtpPlatformAdminEmailSender.from_environment()
platform_admin_email_sender = (
    gmail_email_sender if gmail_email_sender.is_configured else smtp_email_sender
)
APP_STORE_CONNECT_WEBHOOK_SECRET = os.environ.get(
    "APP_STORE_CONNECT_WEBHOOK_SECRET", ""
).strip()
TESTFLIGHT_FEEDBACK_EMAIL = os.environ.get(
    "TESTFLIGHT_FEEDBACK_EMAIL", "kjtsar@kjt.us"
).strip()
TESTFLIGHT_APP_NAME = os.environ.get(
    "TESTFLIGHT_APP_NAME", "RID2Caltopo"
).strip()
TESTFLIGHT_APP_APPLE_ID = os.environ.get(
    "TESTFLIGHT_APP_APPLE_ID", "6792518823"
).strip()
TESTFLIGHT_APP_STORE_CONNECT_URL = (
    f"https://appstoreconnect.apple.com/apps/{TESTFLIGHT_APP_APPLE_ID}/testflight"
)
PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET = os.environ.get(
    "PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET", ""
).strip()
control_plane_tokens = (
    ControlPlaneTokenService(
        CONTROL_PLANE_SIGNING_KEY,
        CONTROL_PLANE_PUBLIC_URL,
    )
    if CONTROL_PLANE_SIGNING_KEY
    else None
)
SECRET_KEY = os.environ.get("SECRET_KEY", False)


def organization_site_ready() -> bool:
    return bool(
        control_plane_store is not None
        and control_plane_tokens is not None
        and SECRET_KEY
        and (CONTROL_PLANE_SIMULATION or SESSION_COOKIE_HTTPS_ONLY)
    )


async def deliver_pending_billing_notifications() -> None:
    if control_plane_store is None or not platform_admin_email_sender.is_configured:
        return
    notifications = await control_plane_store.list_pending_billing_notifications()
    for notification in notifications:
        deadline = (
            notification.deadline_at.strftime("%d %b %Y")
            if notification.deadline_at
            else "the date shown in R2C Tracker"
        )
        try:
            administration_url = (
                f"{CONTROL_PLANE_PUBLIC_URL.rstrip('/')}/"
                f"{notification.designator.lower()}/admin#service-status"
            )
            if notification.notification_type in {
                "beta_allowance_on_track",
                "beta_allowance_exceeded",
                "beta_video_disabled",
            }:
                await asyncio.to_thread(
                    platform_admin_email_sender.send_organization_extended_beta_allowance,
                    recipient=notification.administrator_email,
                    administrator_name=notification.administrator_name,
                    organization_name=notification.organization_name,
                    designator=notification.designator,
                    notification_type=notification.notification_type,
                    month_end=deadline,
                    administration_url=administration_url,
                )
            else:
                await control_plane_store.mark_billing_notification_failed(
                    notification.id,
                    "Unsupported legacy billing notification.",
                )
                continue
            await control_plane_store.mark_billing_notification_sent(notification.id)
        except Exception as exc:
            logging.exception("Organization notification delivery failed")
            await control_plane_store.mark_billing_notification_failed(
                notification.id,
                str(exc),
            )


async def billing_notification_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await reconcile_extended_beta_allowances()
            await deliver_pending_billing_notifications()
        except Exception:
            logging.exception("Extended-beta allowance reconciliation failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=60 * 60)
        except TimeoutError:
            pass


def forecast_organization_cost(actual_cost: Decimal, through: datetime) -> Decimal:
    days_in_month = monthrange(through.year, through.month)[1]
    elapsed_days = max(
        Decimal("1"),
        Decimal(through.day - 1) + Decimal(
            through.hour * 3600 + through.minute * 60 + through.second
        ) / Decimal(86400),
    )
    return (actual_cost * Decimal(days_in_month) / elapsed_days).quantize(
        Decimal("0.01")
    )


async def reconcile_extended_beta_allowances() -> None:
    if control_plane_store is None:
        return
    snapshot, records, usage_aggregates = await asyncio.gather(
        asyncio.to_thread(load_platform_billing_snapshot),
        control_plane_store.list_organizations(),
        control_plane_store.month_to_date_usage_aggregates(),
    )
    if (
        snapshot.source_status != "ready"
        or not snapshot.billing_period_is_current
        or snapshot.billing_data_stale
        or snapshot.billing_period != datetime.now(UTC).strftime("%Y-%m")
    ):
        return
    allocation_inputs = platform_allocation_inputs(records, usage_aggregates)
    allocated_costs, _unallocated = allocate_platform_costs(
        snapshot.actual_cost_breakdown_mtd,
        allocation_inputs,
    )
    actual_costs = {
        organization_id: cost.total
        for organization_id, cost in allocated_costs.items()
    }
    forecasts = {
        organization_id: forecast_organization_cost(
            cost,
            snapshot.billing_data_through,
        )
        for organization_id, cost in actual_costs.items()
    }
    await control_plane_store.reconcile_extended_beta_allowances(
        billing_month=snapshot.billing_period,
        billing_data_through=snapshot.billing_data_through,
        actual_costs=actual_costs,
        forecast_costs=forecasts,
    )


def load_platform_billing_snapshot():
    if PLATFORM_BILLING_SOURCE != "bigquery":
        return build_illustrative_platform_snapshot()
    if not PLATFORM_BILLING_INCLUDED_PROJECTS:
        return build_pending_platform_snapshot(
            "Live billing is enabled, but PLATFORM_BILLING_INCLUDED_PROJECTS "
            "is empty. An explicit allowlist is required so unrelated billing "
            "account costs cannot enter R2C totals.",
            source_status="error",
        )
    try:
        from google.cloud import bigquery

        provider = BigQueryBillingSnapshotProvider(
            client=bigquery.Client(project=PLATFORM_BILLING_PROJECT),
            export_project=PLATFORM_BILLING_PROJECT,
            export_dataset=PLATFORM_BILLING_DATASET,
            included_project_ids=PLATFORM_BILLING_INCLUDED_PROJECTS,
        )
        return provider.load_snapshot()
    except Exception:
        logging.exception("Unable to load aggregate platform billing data")
        return build_pending_platform_snapshot(
            "Google Cloud billing data is temporarily unavailable. No tenant "
            "operational data was queried.",
            source_status="error",
        )


def platform_allocation_inputs(records, usage_aggregates):
    """Build privacy-safe allocation weights for eligible organizations."""
    return {
        record.id: AggregateUsage(
            requests=(
                usage_aggregates[record.id].faa_proxy_requests
                if record.id in usage_aggregates else 0
            ),
            network_bytes=(
                usage_aggregates[record.id].network_bytes
                if record.id in usage_aggregates else 0
            ),
            storage_byte_days=(
                usage_aggregates[record.id].storage_byte_days
                if record.id in usage_aggregates else 0
            ),
            compute_units=(
                usage_aggregates[record.id].compute_units
                if record.id in usage_aggregates else Decimal("0")
            ),
            database_units=(
                usage_aggregates[record.id].database_units
                if record.id in usage_aggregates else Decimal("0")
            ),
            turn_relay_bytes=(
                usage_aggregates[record.id].turn_relay_bytes
                if record.id in usage_aggregates else 0
            ),
        )
        for record in records
        if (
            record.lifecycle_state != "archived"
            and record.provisioning_state == "ready"
        )
    }


def resolve_tracker_version() -> str:
    override = os.environ.get("TRACKER_VERSION")
    if override:
        return override
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            check=True,
            capture_output=True,
            text=True,
            cwd=os.path.dirname(__file__),
        )
        version = result.stdout.strip()
        if version:
            return version
    except Exception:
        pass
    return "unknown"

TRACKER_VERSION = resolve_tracker_version()

BASE_LOG_DIRECTORY = '/flightlogs-vol'
RECORDING_DOWNLOADS_ENABLED = True
RECORDING_DOWNLOAD_SPOOL_TTL_SEC = max(60, int(os.environ.get(
    "RECORDING_DOWNLOAD_SPOOL_TTL_SEC", "3600"
)))
FLIGHTLOGS_STORAGE_REQUIRED = os.environ.get(
    "FLIGHTLOGS_STORAGE_REQUIRED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
faa_notam_proxy = FaaNotamProxy()
R2C_HEARTBEAT_SEC = int(os.environ.get("R2C_HEARTBEAT_SEC", "15"))
R2C_LEASE_SEC = int(os.environ.get("R2C_LEASE_SEC", "45"))
R2C_DB_CLEANUP_SEC = int(os.environ.get("R2C_DB_CLEANUP_SEC", "86400"))
R2C_HEARTBEAT_ZONE_UPDATE_SEC = int(os.environ.get("R2C_HEARTBEAT_ZONE_UPDATE_SEC", "60"))
R2C_IDLE_PARK_SEC = int(os.environ.get("R2C_IDLE_PARK_SEC", "120"))
R2C_RECOMMENDED_APP_VERSION_CODE = int(os.environ.get("R2C_RECOMMENDED_APP_VERSION_CODE", "0") or "0")
R2C_ORGANIZATION_CONFIG_MIN_APP_BUILD = int(
    os.environ.get("R2C_ORGANIZATION_CONFIG_MIN_APP_BUILD", "134") or "134"
)
R2C_TRACKER_FUNCTIONALITY_RELEASE = 148
R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE = int(
    os.environ.get(
        "R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE",
        "0",
    ) or "0"
)
R2C_UPDATE_URL = os.environ.get("R2C_UPDATE_URL", "").strip()
R2C_RECOMMENDED_IOS_APP_BUILD_NUMBER = int(
    os.environ.get("R2C_RECOMMENDED_IOS_APP_BUILD_NUMBER", "0") or "0"
)
R2C_IOS_UPDATE_URL = os.environ.get("R2C_IOS_UPDATE_URL", "").strip()
R2C_COORDINATION_MODE_MAP = "map"
R2C_COORDINATION_MODE_STANDALONE = "standalone"


def _mask_token(token: Optional[str]) -> str:
    if token is None:
        return "<missing>"
    if token == "":
        return "<empty>"
    trimmed = token.strip()
    whitespace_changed = trimmed != token
    if trimmed == "":
        return f"len={len(token)} suffix=<blank> whitespace_changed={whitespace_changed}"
    suffix = trimmed[-4:] if len(trimmed) > 4 else trimmed
    return f"len={len(trimmed)} suffix={suffix} whitespace_changed={whitespace_changed}"


def _describe_tracker_token_mismatch(received: Optional[str], expected: str) -> str:
    trimmed_received = received.strip() if received is not None else None
    trimmed_expected = expected.strip()
    return (
        f"received={_mask_token(received)} "
        f"expected={_mask_token(expected)} "
        f"trimmed_match={trimmed_received == trimmed_expected}"
    )


def _normalize_tracker_token(token: Optional[str]) -> str:
    return token.strip() if token is not None else ""


R2C_SWEEP_SEC = int(os.environ.get("R2C_SWEEP_SEC", "15"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
filepath = __file__
filedate = datetime.fromtimestamp(os.path.getmtime(filepath))
print(f"{filepath} version is {filedate}")

_flight_submission_locks: dict[str, asyncio.Lock] = {}
_flight_submission_locks_guard = asyncio.Lock()


def normalize_flight_submission_key(remote_id: Optional[str], sar_id: Optional[str] = None) -> str:
    remote_key = (remote_id or "").strip().upper()
    if remote_key:
        return f"RID:{remote_key}"
    sar_key = (sar_id or "").strip().upper()
    if sar_key:
        return f"SAR:{sar_key}"
    return ""


@asynccontextmanager
async def serialized_flight_submission(remote_id: Optional[str], sar_id: Optional[str] = None):
    key = normalize_flight_submission_key(remote_id, sar_id)
    async with _flight_submission_locks_guard:
        lock = _flight_submission_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _flight_submission_locks[key] = lock
    async with lock:
        yield


def load_recent_versions(limit: int = 10):
    env_versions = os.environ.get("TRACKER_RECENT_VERSIONS")
    if env_versions:
        try:
            parsed = json.loads(env_versions)
            if isinstance(parsed, list):
                return parsed[:limit]
        except Exception:
            pass

    repo_dir = os.path.dirname(__file__)
    try:
        tags_result = subprocess.run(
            ["git", "tag", "--sort=-creatordate"],
            check=True,
            capture_output=True,
            text=True,
            cwd=repo_dir,
        )
        tags = [tag.strip() for tag in tags_result.stdout.splitlines() if tag.strip()][:limit]
        versions = []
        for tag in tags:
            subject_result = subprocess.run(
                ["git", "log", "-1", "--format=%s", tag],
                check=True,
                capture_output=True,
                text=True,
                cwd=repo_dir,
            )
            date_result = subprocess.run(
                ["git", "log", "-1", "--date=short", "--format=%ad", tag],
                check=True,
                capture_output=True,
                text=True,
                cwd=repo_dir,
            )
            versions.append({
                "tag": tag,
                "date": date_result.stdout.strip(),
                "summary": subject_result.stdout.strip(),
            })
        return versions
    except Exception:
        return []

# connector no longer needed when running locally in same VM.
def getconn():
    connector = Connector()
    return connector.connect(DB_URL, "pg8000", user=DB_USER, password=DB_PASS, db=DB_NAME, ip_type=IPTypes.PUBLIC)
# engine = create_engine("postgresql+pg8000://", creator=getconn)
# engine = create_engine(DB_URL)
# SessionLocal = sessionmaker(autocommit= False, autoflush=False, bind=engine)
# Base.metadata.create_all(bind=engine)

Base = declarative_base()
LEGACY_COORDINATION_ORGANIZATION_ID = "legacy"


class Flight(Base):
    __tablename__ = "flights"
    id = Column(Integer, primary_key=True)
    organization_id = Column(String(36), nullable=True, index=True)
    sar_id = Column(String, default="undefined")
    remote_id = Column(String, default="", index=True)
    uas = Column(String, default="")
    incident = Column(String, default="")
    op_period = Column(String, default="")
    map_id = Column(String, default="")
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    start_lat = Column(Float, default = 0.0)
    start_lng = Column(Float, default = 0.0)
    hours = Column(Float, default=0.0)
    distance_mi = Column(Float, default=0.0)
    temp_f = Column(Float, default=0.0)
    rhum_pct = Column(Float, default=0.0)
    dewpt_f = Column(Float, default=0.0)
    precip_in = Column(Float, default=0.0)
    wind_mph = Column(Float, default=0.0)
    gusts_mph = Column(Float, default=0.0)
    cloudcvr_pct = Column(Float, default=0.0)
    timeofday = Column(String, default="day")
    archive_relpath = Column(String, default="")


class R2CZoneState(Base):
    __tablename__ = "r2c_zone_state"
    id = Column(Integer, primary_key=True)
    organization_id = Column(
        String(36),
        index=True,
        nullable=False,
        default=LEGACY_COORDINATION_ORGANIZATION_ID,
    )
    map_id = Column(String, index=True, nullable=False)
    reported_map_id = Column(String, default="")
    coordination_mode = Column(String, default=R2C_COORDINATION_MODE_MAP)
    zone_id = Column(String, index=True, nullable=False)
    guid = Column(String, index=True, nullable=False)
    name = Column(String, default="")
    app_version = Column(String, default="")
    app_version_code = Column(Integer, default=0)
    lat = Column(Float, default=0.0)
    lng = Column(Float, default=0.0)
    caltopo_rtt_ms = Column(Integer, default=0)
    online = Column(Boolean, default=True)
    connection_state = Column(String, default="online")
    last_seen_ms = Column(BigInteger, default=0)


class R2CDroneOwnerState(Base):
    __tablename__ = "r2c_drone_owner_state"
    id = Column(Integer, primary_key=True)
    organization_id = Column(
        String(36),
        index=True,
        nullable=False,
        default=LEGACY_COORDINATION_ORGANIZATION_ID,
    )
    map_id = Column(String, index=True, nullable=False)
    remote_id = Column(String, index=True, nullable=False)
    owner_guid = Column(String, default="")
    owner_zone_id = Column(String, default="")
    first_drone_ts = Column(BigInteger, default=0)
    first_distance_m = Column(Float, default=0.0)
    mapped_id = Column(String, default="")
    lease_seq = Column(Integer, default=0)
    lease_expire_ms = Column(BigInteger, default=0)
    updated_ms = Column(BigInteger, default=0)


class R2CDroneConfirmationState(Base):
    __tablename__ = "r2c_drone_confirmation_state"
    id = Column(Integer, primary_key=True)
    organization_id = Column(
        String(36),
        index=True,
        nullable=False,
        default=LEGACY_COORDINATION_ORGANIZATION_ID,
    )
    map_id = Column(String, index=True, nullable=False)
    remote_id = Column(String, index=True, nullable=False)
    zone_id = Column(String, default="")
    guid = Column(String, default="")
    confirmed_by_guid = Column(String, default="")
    mapped_id = Column(String, default="")
    track_label = Column(String, default="")
    org = Column(String, default="")
    model = Column(String, default="")
    owner_name = Column(String, default="")
    confirmed_at_ms = Column(BigInteger, default=0)


class R2CRecentSighting(Base):
    __tablename__ = "r2c_recent_sighting"
    id = Column(Integer, primary_key=True)
    organization_id = Column(
        String(36),
        index=True,
        nullable=False,
        default=LEGACY_COORDINATION_ORGANIZATION_ID,
    )
    map_id = Column(String, index=True, nullable=False)
    remote_id = Column(String, index=True, nullable=False)
    zone_id = Column(String, default="")
    guid = Column(String, default="")
    drone_ts = Column(BigInteger, default=0)
    lat = Column(Float, default=0.0)
    lng = Column(Float, default=0.0)
    alt_m = Column(Float, default=0.0)
    received_ms = Column(BigInteger, default=0)

engine = create_async_engine(DB_URL, echo=True)


async def migrate_r2c_coordination_schema():
    # Existing production tables may have been created with 32-bit INTEGER timestamp columns.
    # Coordination state now stores epoch milliseconds, which require BIGINT.
    timestamp_columns = [
        ("r2c_zone_state", "last_seen_ms"),
        ("r2c_drone_owner_state", "first_drone_ts"),
        ("r2c_drone_owner_state", "lease_expire_ms"),
        ("r2c_drone_owner_state", "updated_ms"),
        ("r2c_drone_confirmation_state", "confirmed_at_ms"),
        ("r2c_recent_sighting", "drone_ts"),
        ("r2c_recent_sighting", "received_ms"),
    ]
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        coordination_tables = (
            "r2c_zone_state",
            "r2c_drone_owner_state",
            "r2c_drone_confirmation_state",
            "r2c_recent_sighting",
        )
        coordination_scope_indexes = {
            "r2c_zone_state": "organization_id, map_id, zone_id",
            "r2c_drone_owner_state": "organization_id, map_id, remote_id",
            "r2c_drone_confirmation_state": "organization_id, map_id, remote_id",
            "r2c_recent_sighting": "organization_id, map_id, remote_id",
        }
        if dialect == "postgresql":
            for table_name, column_name in timestamp_columns:
                result = await conn.execute(text("""
                    SELECT data_type
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = :table_name
                      AND column_name = :column_name
                """), {
                    "table_name": table_name,
                    "column_name": column_name,
                })
                data_type = result.scalar_one_or_none()
                if data_type and data_type != "bigint":
                    logger.warning(
                        "Migrating %s.%s from %s to BIGINT for coordination timestamps",
                        table_name,
                        column_name,
                        data_type,
                    )
                    await conn.execute(text(
                        f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE BIGINT"
                    ))
        elif dialect == "sqlite":
            # SQLite INTEGER is already 64-bit and does not need migration here.
            pass
        if dialect == "postgresql":
            for table_name in coordination_tables:
                result = await conn.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = :table_name
                """), {"table_name": table_name})
                table_columns = {row[0] for row in result.fetchall()}
                if "organization_id" not in table_columns:
                    await conn.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN organization_id "
                        f"VARCHAR(36) NOT NULL DEFAULT '{LEGACY_COORDINATION_ORGANIZATION_ID}'"
                    ))
                await conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_organization_id "
                    f"ON {table_name} (organization_id)"
                ))
                await conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_scope "
                    f"ON {table_name} ({coordination_scope_indexes[table_name]})"
                ))
            result = await conn.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'r2c_zone_state'
            """))
            columns = {row[0] for row in result.fetchall()}
            if "reported_map_id" not in columns:
                await conn.execute(text("ALTER TABLE r2c_zone_state ADD COLUMN reported_map_id VARCHAR DEFAULT ''"))
            if "coordination_mode" not in columns:
                await conn.execute(text(
                    f"ALTER TABLE r2c_zone_state ADD COLUMN coordination_mode VARCHAR DEFAULT '{R2C_COORDINATION_MODE_MAP}'"
                ))
            if "connection_state" not in columns:
                await conn.execute(text("ALTER TABLE r2c_zone_state ADD COLUMN connection_state VARCHAR DEFAULT 'online'"))
            if "app_version" not in columns:
                await conn.execute(text("ALTER TABLE r2c_zone_state ADD COLUMN app_version VARCHAR DEFAULT ''"))
            if "app_version_code" not in columns:
                await conn.execute(text("ALTER TABLE r2c_zone_state ADD COLUMN app_version_code INTEGER DEFAULT 0"))
        elif dialect == "sqlite":
            for table_name in coordination_tables:
                result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                table_columns = {row[1] for row in result.fetchall()}
                if "organization_id" not in table_columns:
                    await conn.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN organization_id "
                        f"TEXT NOT NULL DEFAULT '{LEGACY_COORDINATION_ORGANIZATION_ID}'"
                    ))
                await conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_organization_id "
                    f"ON {table_name} (organization_id)"
                ))
                await conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_scope "
                    f"ON {table_name} ({coordination_scope_indexes[table_name]})"
                ))
            result = await conn.execute(text("PRAGMA table_info(r2c_zone_state)"))
            columns = {row[1] for row in result.fetchall()}
            if "reported_map_id" not in columns:
                await conn.execute(text("ALTER TABLE r2c_zone_state ADD COLUMN reported_map_id TEXT DEFAULT ''"))
            if "coordination_mode" not in columns:
                await conn.execute(text(
                    f"ALTER TABLE r2c_zone_state ADD COLUMN coordination_mode TEXT DEFAULT '{R2C_COORDINATION_MODE_MAP}'"
                ))
            if "connection_state" not in columns:
                await conn.execute(text("ALTER TABLE r2c_zone_state ADD COLUMN connection_state TEXT DEFAULT 'online'"))
            if "app_version" not in columns:
                await conn.execute(text("ALTER TABLE r2c_zone_state ADD COLUMN app_version TEXT DEFAULT ''"))
            if "app_version_code" not in columns:
                await conn.execute(text("ALTER TABLE r2c_zone_state ADD COLUMN app_version_code INTEGER DEFAULT 0"))


async def migrate_flight_archive_schema():
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            result = await conn.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'flights'
            """))
            columns = {row[0] for row in result.fetchall()}
            if "start_lat" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN start_lat DOUBLE PRECISION DEFAULT 0.0"))
            if "start_lng" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN start_lng DOUBLE PRECISION DEFAULT 0.0"))
            if "timeofday" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN timeofday VARCHAR DEFAULT 'day'"))
            if "archive_relpath" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN archive_relpath VARCHAR DEFAULT ''"))
            if "remote_id" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN remote_id VARCHAR DEFAULT ''"))
            if "organization_id" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN organization_id VARCHAR(36)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_flights_remote_id ON flights (remote_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_flights_organization_id ON flights (organization_id)"))
        elif dialect == "sqlite":
            result = await conn.execute(text("PRAGMA table_info(flights)"))
            columns = {row[1] for row in result.fetchall()}
            if "start_lat" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN start_lat FLOAT DEFAULT 0.0"))
            if "start_lng" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN start_lng FLOAT DEFAULT 0.0"))
            if "timeofday" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN timeofday TEXT DEFAULT 'day'"))
            if "archive_relpath" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN archive_relpath TEXT DEFAULT ''"))
            if "remote_id" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN remote_id TEXT DEFAULT ''"))
            if "organization_id" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN organization_id TEXT"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_flights_remote_id ON flights (remote_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_flights_organization_id ON flights (organization_id)"))


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_r2c_coordination_schema()
    await migrate_flight_archive_schema()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)


def _remove_expired_recording_spool_files(relative_paths: tuple[str, ...]) -> int:
    removed = 0
    recordings_root = os.path.join(BASE_LOG_DIRECTORY, "organizations")
    for relative_path in relative_paths:
        normalized = os.path.normpath(relative_path)
        if os.path.isabs(normalized) or normalized == ".." or normalized.startswith(".." + os.sep):
            logger.warning("Refusing unsafe recording spool path: %s", relative_path)
            continue
        try:
            os.remove(os.path.join(BASE_LOG_DIRECTORY, normalized))
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Unable to remove expired recording spool %s: %s", relative_path, exc)

    # Recover from interrupted uploads or a process failure between deleting a
    # file and updating its database row. Everything below recordings/ is a
    # bounded transfer spool, so age is sufficient to reap orphaned files.
    cutoff = datetime.now(UTC).timestamp() - RECORDING_DOWNLOAD_SPOOL_TTL_SEC
    if os.path.isdir(recordings_root):
        for directory, _subdirectories, filenames in os.walk(recordings_root):
            if f"{os.sep}recordings{os.sep}" not in directory + os.sep:
                continue
            for filename in filenames:
                path = os.path.join(directory, filename)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning("Unable to reap recording spool %s: %s", path, exc)
    return removed


async def cleanup_recording_download_spools() -> int:
    if control_plane_store is None:
        return 0
    relative_paths = await control_plane_store.expire_recording_download_spools()
    return await anyio.to_thread.run_sync(
        _remove_expired_recording_spool_files, relative_paths,
    )


async def recording_spool_cleanup_worker(stop: asyncio.Event) -> None:
    interval_seconds = max(30, min(300, RECORDING_DOWNLOAD_SPOOL_TTL_SEC // 4))
    while not stop.is_set():
        try:
            removed = await cleanup_recording_download_spools()
            if removed:
                logger.info("Removed %s expired recording transfer spool files", removed)
        except Exception as exc:
            logger.warning("Recording transfer spool cleanup failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass


async def audit_retention_cleanup_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            if control_plane_store is not None:
                removed = await control_plane_store.purge_expired_audit_events()
                if removed:
                    logger.info(
                        "Removed %s audit events older than %s days",
                        removed,
                        AUDIT_EVENT_RETENTION_DAYS,
                    )
        except Exception as exc:
            logger.warning("Audit retention cleanup failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=24 * 60 * 60)
        except asyncio.TimeoutError:
            pass

if __name__ == "__main__":
    asyncio.run(init_db())

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup Create tables
    await init_db()
    if control_plane_store is not None:
        await control_plane_store.init()
    billing_notification_stop = asyncio.Event()
    billing_notification_task = None
    recording_spool_cleanup_stop = asyncio.Event()
    recording_spool_cleanup_task = asyncio.create_task(
        recording_spool_cleanup_worker(recording_spool_cleanup_stop)
    )
    audit_retention_cleanup_stop = asyncio.Event()
    audit_retention_cleanup_task = asyncio.create_task(
        audit_retention_cleanup_worker(audit_retention_cleanup_stop)
    )
    if control_plane_store is not None:
        billing_notification_task = asyncio.create_task(
            billing_notification_worker(billing_notification_stop)
        )
    await r2c_hub.start()
    yield
    # Shutdown Clean up resources (if needed)
    await r2c_hub.stop()
    billing_notification_stop.set()
    recording_spool_cleanup_stop.set()
    audit_retention_cleanup_stop.set()
    if billing_notification_task is not None:
        await billing_notification_task
    await recording_spool_cleanup_task
    await audit_retention_cleanup_task
    if control_plane_store is not None:
        await control_plane_store.dispose()
    await engine.dispose()

app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="r2c_tracker_session_v2",
    https_only=SESSION_COOKIE_HTTPS_ONLY,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def protect_control_plane_pages(request: Request, call_next):
    response = await call_next(request)
    if SESSION_COOKIE_HTTPS_ONLY:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    path = request.url.path
    if path.startswith("/static/") and path.endswith(".js"):
        response.headers["Cache-Control"] = "no-cache, max-age=0"
    organization_page = re.fullmatch(
        r"/[a-z0-9]{2,16}/(?:"
        r"activate(?:/(?:google|microsoft))?|login|forgot-password|reset-password|"
        r"(?:google|microsoft)/start|logout|admin(?:/.*)?|settings|members|"
        r"streams(?:/status|/[^/]+/request|/requests/[^/]+/"
        r"(?:cancel|preflight/(?:offer|status)))?|"
        r"enroll(?:/credential)?|"
        r"enrollments(?:/[^/]+/(?:revoke|qr\.svg))?"
        r")",
        path,
    )
    if (
        path.startswith("/platform-admin/")
        or path == "/google/callback"
        or path == "/microsoft/callback"
        or path == "/login"
        or path == "/organizations/select"
        or (
            path == "/"
            and request.session.get("organization_user_id")
        )
        or organization_page is not None
    ):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "form-action 'self' https://accounts.google.com "
            "https://login.microsoftonline.com; "
            "frame-ancestors 'none'; "
            "base-uri 'none'"
        )
    return response


class OrganizationStreamEventHub:
    """Fan out privacy-minimal stream lifecycle changes by organization."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._connections: dict[
            str, dict[WebSocket, tuple[str, str]]
        ] = {}

    async def connect(
        self,
        organization_id: str,
        organization_designator: str,
        user_email: str,
        websocket: WebSocket,
    ):
        await websocket.accept()
        async with self._lock:
            self._connections.setdefault(organization_id, {})[websocket] = (
                organization_designator,
                user_email,
            )

    async def disconnect(self, organization_id: str, websocket: WebSocket):
        async with self._lock:
            connections = self._connections.get(organization_id)
            if connections is None:
                return
            connections.pop(websocket, None)
            if not connections:
                self._connections.pop(organization_id, None)

    async def broadcast(self, organization_id: str):
        async with self._lock:
            connections = tuple(self._connections.get(organization_id, {}))
        failed = []
        for websocket in connections:
            try:
                await websocket.send_json({"type": "streams_changed"})
            except Exception:
                failed.append(websocket)
        for websocket in failed:
            await self.disconnect(organization_id, websocket)

    async def connection_count(self) -> int:
        async with self._lock:
            return sum(len(connections) for connections in self._connections.values())

    async def deployment_connection_details(self) -> list[dict]:
        async with self._lock:
            return sorted(
                (
                    {
                        "organization": designator,
                        "user": user_email,
                    }
                    for connections in self._connections.values()
                    for designator, user_email in connections.values()
                ),
                key=lambda item: (item["organization"], item["user"]),
            )


organization_stream_event_hub = OrganizationStreamEventHub()


class R2CZoneConnection:
    def __init__(
        self,
        websocket: Optional[WebSocket],
        device_credential: Optional[object] = None,
        organization_id: str = LEGACY_COORDINATION_ORGANIZATION_ID,
    ):
        self.websocket = websocket
        self.device_credential = device_credential
        self.organization_id = organization_id
        self.map_id: Optional[str] = None
        self.reported_map_id: str = ""
        self.coordination_mode: str = "map"
        self.zone_id: Optional[str] = None
        self.guid: Optional[str] = None
        self.name: str = ""
        self.app_version: str = ""
        self.app_version_code: int = 0
        self.lat: float = 0.0
        self.lng: float = 0.0
        self.caltopo_rtt_ms: int = 0
        self.connection_state: str = "online"
        self.connected_at_ms: int = 0
        self.hello_received_at_ms: int = 0
        self.last_seen_ms: int = 0
        self.remote_video_control_enabled: bool = False
        self.video_inventory_reconciled: bool = False
        self.sent_confirmed_event_keys: set[str] = set()


class ConnectedConfigSource:
    def __init__(
        self,
        *,
        id: str,
        organization_id: str,
        designator: str,
        device_name: str,
        platform: str,
        app_version: str,
        app_version_code: int,
    ):
        self.id = id
        self.organization_id = organization_id
        self.designator = designator
        self.device_name = device_name
        self.platform = platform
        self.app_version = app_version
        self.app_version_code = app_version_code

    @property
    def supports_organization_config(self) -> bool:
        return self.app_version_code >= R2C_ORGANIZATION_CONFIG_MIN_APP_BUILD


def organization_config_upgrade_message(source: ConnectedConfigSource) -> str:
    installed = (
        f"build {source.app_version_code}"
        if source.app_version_code > 0
        else "software that does not report its build number"
    )
    return (
        f"{source.device_name} is running {installed} and cannot return its "
        "organization configuration. Upgrade RID2Caltopo to build "
        f"{R2C_ORGANIZATION_CONFIG_MIN_APP_BUILD} or later."
    )


class R2CCoordinationHub:
    STANDALONE_PREFIX = "Standalone_"
    STANDALONE_GROUP_RADIUS_M = 2.0 * 1609.344
    CONFIRMATION_RETENTION_MS = 12 * 60 * 60 * 1000
    COORDINATION_MODE_MAP = "map"
    COORDINATION_MODE_STANDALONE = "standalone"
    VIDEO_THUMBNAIL_MAX_BYTES = 256 * 1024
    VIDEO_THUMBNAIL_TTL_SECONDS = 90

    def __init__(self):
        self._lock = asyncio.Lock()
        self._connections: dict[WebSocket, R2CZoneConnection] = {}
        self._connections_by_device_credential_id: dict[
            str, R2CZoneConnection
        ] = {}
        self._zones_by_map: dict[tuple[str, str], dict[str, R2CZoneConnection]] = {}
        self._owners: dict[tuple[str, str, str], dict] = {}
        self._confirmed_drones_by_map: dict[tuple[str, str], dict[str, dict]] = {}
        self._confirmation_event_seq: int = 0
        self._last_heartbeat_zone_update_ms_by_map: dict[tuple[str, str], int] = {}
        self._sweep_task: Optional[asyncio.Task] = None
        self._load_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._video_notification_task: Optional[asyncio.Task] = None
        # Short-lived JPEGs support browser cards and CalTopo's conventional
        # thumbnail_url. They are never written to the database or filesystem.
        self._video_thumbnails: dict[
            tuple[str, str], tuple[bytes, str, datetime]
        ] = {}

    async def resolve_tablet_link_code(
        self,
        code: str,
    ) -> Optional["DeviceCredentialRecord"]:
        """Resolve a short alias strictly from authenticated live sockets.

        A 32-bit code can collide. Never guess in that case: an ambiguous
        alias is unavailable until only one matching tablet remains connected.
        """
        clean_code = code.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{6}", clean_code):
            return None
        async with self._lock:
            matches: dict[str, "DeviceCredentialRecord"] = {}
            for connection in self._connections.values():
                credential = connection.device_credential
                if credential is None:
                    continue
                expected = tablet_link_code(
                    credential.designator,
                    credential.device_name,
                )
                if secrets.compare_digest(clean_code, expected):
                    matches[credential.id] = credential
            if len(matches) != 1:
                if len(matches) > 1:
                    logger.warning(
                        "Ambiguous live tablet alias rejected: code=%s matches=%s",
                        clean_code,
                        len(matches),
                    )
                return None
            return next(iter(matches.values()))

    async def resolve_connected_tablet(
        self,
        designator: str,
        device_name: str,
    ) -> Optional["DeviceCredentialRecord"]:
        """Resolve one canonical tablet path from live connection state."""
        clean_designator = designator.strip().lower()
        clean_device_name = device_name.strip().lower()
        if not clean_designator or not clean_device_name:
            return None
        async with self._lock:
            matches: dict[str, "DeviceCredentialRecord"] = {}
            for connection in self._connections.values():
                credential = connection.device_credential
                if (
                    credential is not None
                    and credential.designator.strip().lower() == clean_designator
                    and credential.device_name.strip().lower() == clean_device_name
                ):
                    matches[credential.id] = credential
            if len(matches) != 1:
                return None
            return next(iter(matches.values()))

    async def list_connected_tablets(
        self,
        organization_id: str,
    ) -> tuple["DeviceCredentialRecord", ...]:
        """List authenticated live R2C tablets for one organization."""
        async with self._lock:
            matches = {
                connection.device_credential.id: connection.device_credential
                for connection in self._connections.values()
                if connection.device_credential is not None
                and connection.organization_id == organization_id
            }
        return tuple(sorted(
            matches.values(),
            key=lambda credential: (
                credential.device_name.casefold(),
                credential.id,
            ),
        ))

    async def deployment_connection_details(self) -> list[dict]:
        """Describe authenticated tablets currently connected to this revision."""
        async with self._lock:
            details = [
                {
                    "organization": connection.device_credential.designator,
                    "device": connection.device_credential.device_name,
                    "platform": connection.device_credential.platform,
                    "device_credential_id": connection.device_credential.id,
                    "map_id": connection.map_id,
                    "zone_id": connection.zone_id,
                    "app_version": connection.app_version,
                    "app_version_code": connection.app_version_code,
                }
                for connection in self._connections.values()
                if connection.device_credential is not None
            ]
        return sorted(
            details,
            key=lambda item: (
                item["organization"],
                item["device"],
                item["zone_id"],
            ),
        )

    async def list_connected_config_sources(
        self,
        organization_id: str,
    ) -> tuple[ConnectedConfigSource, ...]:
        """List live tablets with the app build needed for config compatibility."""
        async with self._lock:
            latest_by_credential_id: dict[str, R2CZoneConnection] = {}
            for connection in self._connections.values():
                credential = connection.device_credential
                if credential is None or connection.organization_id != organization_id:
                    continue
                current = latest_by_credential_id.get(credential.id)
                if (
                    current is None
                    or connection.hello_received_at_ms > current.hello_received_at_ms
                ):
                    latest_by_credential_id[credential.id] = connection
            sources = tuple(
                ConnectedConfigSource(
                    id=connection.device_credential.id,
                    organization_id=connection.device_credential.organization_id,
                    designator=connection.device_credential.designator,
                    device_name=connection.device_credential.device_name,
                    platform=connection.device_credential.platform,
                    app_version=connection.app_version,
                    app_version_code=connection.app_version_code,
                )
                for connection in latest_by_credential_id.values()
            )
        return tuple(sorted(
            sources,
            key=lambda source: (source.device_name.casefold(), source.id),
        ))

    async def resolve_stream_link_code(self, code: str):
        """Resolve one captured stream alias from live tablet advertisements."""
        clean_code = code.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{6}", clean_code):
            return None
        async with self._lock:
            credentials = {
                connection.device_credential.id: connection.device_credential
                for connection in self._connections.values()
                if connection.device_credential is not None
            }
        matches = []
        for credential in credentials.values():
            streams = await control_plane_store.list_active_video_streams(
                credential.organization_id
            )
            for stream in streams:
                if stream.device_credential_id != credential.id:
                    continue
                expected = stream_link_code(
                    credential.designator,
                    credential.device_name,
                    stream.drone_designator,
                )
                if secrets.compare_digest(clean_code, expected):
                    matches.append((credential, stream))
        unique = {
            (credential.id, stream.drone_designator.strip().lower()): (
                credential,
                stream,
            )
            for credential, stream in matches
        }
        if len(unique) != 1:
            if len(unique) > 1:
                logger.warning(
                    "Ambiguous captured stream alias rejected: code=%s matches=%s",
                    clean_code,
                    len(unique),
                )
            return None
        return next(iter(unique.values()))

    async def resolve_recording_link_code(self, code: str):
        """Resolve a short alias to exactly one advertised recording."""
        clean_code = code.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{6}", clean_code):
            return None
        async with self._lock:
            credentials = {
                connection.device_credential.id: connection.device_credential
                for connection in self._connections.values()
                if connection.device_credential is not None
            }
        matches = []
        for credential in credentials.values():
            streams = await control_plane_store.list_active_video_streams(
                credential.organization_id
            )
            for stream in streams:
                if (
                    stream.device_credential_id != credential.id
                    or stream.media_kind != "recording"
                ):
                    continue
                expected = recording_link_code(
                    credential.designator,
                    credential.device_name,
                    stream.session_id,
                )
                if secrets.compare_digest(clean_code, expected):
                    matches.append((credential, stream))
        unique = {
            (credential.id, stream.session_id): (credential, stream)
            for credential, stream in matches
        }
        return next(iter(unique.values())) if len(unique) == 1 else None

    async def cache_video_thumbnail(
        self,
        *,
        device_credential_id: str,
        session_id: str,
        revision: str,
        jpeg_base64: str,
    ) -> bool:
        clean_revision = revision.strip()[:64]
        encoded = jpeg_base64.strip()
        if not clean_revision or not encoded:
            return False
        try:
            jpeg = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return False
        if (
            len(jpeg) < 4
            or len(jpeg) > self.VIDEO_THUMBNAIL_MAX_BYTES
            or not jpeg.startswith(b"\xff\xd8")
            or not jpeg.endswith(b"\xff\xd9")
        ):
            return False
        expires_at = datetime.now(UTC) + timedelta(
            seconds=self.VIDEO_THUMBNAIL_TTL_SECONDS
        )
        async with self._lock:
            self._video_thumbnails[(device_credential_id, session_id)] = (
                jpeg,
                clean_revision,
                expires_at,
            )
        return True

    async def video_thumbnail(
        self,
        *,
        device_credential_id: str,
        session_id: str,
        revision: str = "",
    ) -> Optional[bytes]:
        now = datetime.now(UTC)
        async with self._lock:
            key = (device_credential_id, session_id)
            cached = self._video_thumbnails.get(key)
            if cached is None:
                return None
            jpeg, cached_revision, expires_at = cached
            if expires_at < now:
                self._video_thumbnails.pop(key, None)
                return None
            if revision and not secrets.compare_digest(
                revision[:64], cached_revision
            ):
                return None
            return jpeg

    async def start(self):
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._expiry_loop())
        if self._load_task is None:
            self._load_task = asyncio.create_task(self._load_state_safe())
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        if (
            self._video_notification_task is None
            and CONTROL_PLANE_DATABASE_URL.startswith("postgresql")
        ):
            self._video_notification_task = asyncio.create_task(
                self._video_notification_loop()
            )

    async def stop(self):
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None
        if self._load_task is not None:
            self._load_task.cancel()
            try:
                await self._load_task
            except asyncio.CancelledError:
                pass
            self._load_task = None
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        if self._video_notification_task is not None:
            self._video_notification_task.cancel()
            try:
                await self._video_notification_task
            except asyncio.CancelledError:
                pass
            self._video_notification_task = None

    async def _video_notification_loop(self):
        dsn = re.sub(
            r"^postgresql\+[^:]+:",
            "postgresql:",
            CONTROL_PLANE_DATABASE_URL,
        )
        retry_seconds = 1
        while True:
            connection = None
            wake = asyncio.Event()

            def receive_notification(
                _connection,
                _process_id,
                channel,
                payload,
            ):
                if channel == "r2c_stream_change":
                    asyncio.create_task(
                        organization_stream_event_hub.broadcast(payload.strip())
                    )
                elif channel == "r2c_video_thumbnail_preview":
                    asyncio.create_task(
                        self._deliver_notified_video_thumbnail_preview(payload)
                    )
                elif channel == "r2c_video_preflight":
                    asyncio.create_task(
                        self._deliver_notified_video_preflight_offer(payload)
                    )
                elif channel == "r2c_video_media":
                    asyncio.create_task(
                        self._deliver_notified_video_media_offer(payload)
                    )
                elif channel == "r2c_recording_download":
                    asyncio.create_task(
                        self._deliver_notified_recording_download(payload)
                    )
                else:
                    asyncio.create_task(
                        self._deliver_notified_video_stream_request(payload)
                    )

            try:
                connection = await asyncpg.connect(dsn)
                await connection.add_listener(
                    "r2c_video_request",
                    receive_notification,
                )
                await connection.add_listener(
                    "r2c_video_preflight",
                    receive_notification,
                )
                await connection.add_listener(
                    "r2c_video_media",
                    receive_notification,
                )
                await connection.add_listener(
                    "r2c_stream_change",
                    receive_notification,
                )
                await connection.add_listener(
                    "r2c_video_thumbnail_preview",
                    receive_notification,
                )
                await connection.add_listener(
                    "r2c_recording_download",
                    receive_notification,
                )
                retry_seconds = 1
                await wake.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Managed video notification listener disconnected; "
                    "presence replay remains active",
                    exc_info=True,
                )
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 30)
            finally:
                if connection is not None:
                    try:
                        await connection.close()
                    except Exception:
                        logger.debug(
                            "Managed video notification connection close failed",
                            exc_info=True,
                        )

    async def _deliver_notified_video_thumbnail_preview(self, payload: str):
        try:
            message = json.loads(payload)
            await self.send_video_thumbnail_preview(
                device_credential_id=str(
                    message.get("deviceCredentialId", "")
                ).strip(),
                ttl_seconds=int(message.get("ttlSec", 25) or 25),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Invalid managed thumbnail preview notification")

    async def _deliver_notified_video_stream_request(
        self,
        request_id: str,
    ):
        if control_plane_store is None:
            return
        try:
            stream_request = (
                await control_plane_store.get_pending_video_stream_request(
                    request_id.strip()
                )
            )
        except Exception:
            logger.warning(
                "Managed video notification lookup failed",
                exc_info=True,
            )
            return
        if stream_request is None:
            return
        await self.send_video_stream_request(
            device_credential_id=stream_request.device_credential_id,
            request_id=stream_request.id,
            requester_email=stream_request.requester_email,
            stream_session_id=stream_request.stream_session_id,
            incident_name=stream_request.incident_name,
            drone_designator=stream_request.drone_designator,
            source_width=stream_request.source_width,
            source_height=stream_request.source_height,
            source_fps=stream_request.source_fps,
            source_bitrate_bps=stream_request.source_bitrate_bps,
            source_codec=stream_request.source_codec,
            remote_control_enabled=stream_request.remote_control_enabled,
            expires_at=stream_request.expires_at,
        )

    async def _deliver_notified_recording_download(self, request_id: str):
        if control_plane_store is None:
            return
        try:
            item = await control_plane_store.get_recording_download_request(
                request_id=request_id.strip()
            )
        except ControlPlaneError:
            return
        await self.send_recording_download_request(item)

    async def _deliver_notified_video_preflight_offer(
        self,
        request_id: str,
    ):
        if control_plane_store is None:
            return
        try:
            exchange = (
                await control_plane_store.get_pending_video_preflight_offer(
                    request_id=request_id,
                )
            )
        except Exception:
            logger.warning(
                "Managed video preflight notification lookup failed",
                exc_info=True,
            )
            return
        if exchange is None:
            return
        await self.send_video_preflight_offer(exchange)

    async def _deliver_notified_video_media_offer(self, request_id: str):
        if control_plane_store is None:
            return
        try:
            exchange = await control_plane_store.get_pending_video_media_offer(
                request_id=request_id.strip()
            )
        except Exception:
            logger.warning("Managed video media notification lookup failed", exc_info=True)
            return
        if exchange is not None:
            await self.send_video_media_offer(exchange)

    @classmethod
    def _is_standalone_reported_map_id(cls, reported_map_id: str) -> bool:
        normalized = (reported_map_id or "").strip()
        return (
            normalized == ""
            or normalized.startswith("profile:")
            or normalized.startswith(cls.STANDALONE_PREFIX)
        )

    @classmethod
    def _sanitize_standalone_key(cls, value: str) -> str:
        key = re.sub(r"[^A-Za-z0-9_-]+", "_", value or "").strip("_")
        return key or "isolated"

    @staticmethod
    def _has_usable_location(lat: float, lng: float) -> bool:
        return (
            math.isfinite(lat)
            and math.isfinite(lng)
            and -90.0 <= lat <= 90.0
            and -180.0 <= lng <= 180.0
            and not (abs(lat) < 0.000001 and abs(lng) < 0.000001)
        )

    @staticmethod
    def _distance_meters(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
        radius_m = 6371000.0
        phi_a = math.radians(lat_a)
        phi_b = math.radians(lat_b)
        delta_phi = math.radians(lat_b - lat_a)
        delta_lambda = math.radians(lng_b - lng_a)
        haversine = (
            math.sin(delta_phi / 2.0) ** 2
            + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2.0) ** 2
        )
        return 2.0 * radius_m * math.atan2(math.sqrt(haversine), math.sqrt(1.0 - haversine))

    @classmethod
    def _standalone_map_id_for_zone(cls, zone_id: str, guid: str) -> str:
        return f"{cls.STANDALONE_PREFIX}{cls._sanitize_standalone_key(zone_id or guid)}"

    @staticmethod
    def _scope_key(organization_id: str, map_id: str) -> tuple[str, str]:
        return organization_id, map_id

    def _resolve_coordination_map_id(self, organization_id: str, reported_map_id: str, zone_id: str, guid: str,
                                     lat: float, lng: float) -> tuple[str, str]:
        normalized = (reported_map_id or "").strip()
        if not self._is_standalone_reported_map_id(normalized):
            return normalized, self.COORDINATION_MODE_MAP
        fallback_map_id = self._standalone_map_id_for_zone(zone_id, guid)
        if not self._has_usable_location(lat, lng):
            return fallback_map_id, self.COORDINATION_MODE_STANDALONE

        nearby_mapped: list[tuple[float, str]] = []
        nearby_standalone: list[tuple[float, str]] = []
        for (candidate_organization_id, map_id), zones in self._zones_by_map.items():
            if candidate_organization_id != organization_id:
                continue
            for zone in zones.values():
                if zone.zone_id == zone_id:
                    continue
                if zone.websocket is None:
                    continue
                if not self._has_usable_location(zone.lat, zone.lng):
                    continue
                distance_m = self._distance_meters(lat, lng, zone.lat, zone.lng)
                if distance_m > self.STANDALONE_GROUP_RADIUS_M:
                    continue
                if (
                    not map_id.startswith(self.STANDALONE_PREFIX)
                    and zone.coordination_mode == self.COORDINATION_MODE_MAP
                ):
                    nearby_mapped.append((distance_m, map_id))
                elif (
                    map_id.startswith(self.STANDALONE_PREFIX)
                    and zone.coordination_mode == self.COORDINATION_MODE_STANDALONE
                ):
                    nearby_standalone.append((distance_m, map_id))
        if nearby_mapped:
            nearby_mapped.sort(key=lambda row: (row[0], row[1]))
            return nearby_mapped[0][1], self.COORDINATION_MODE_STANDALONE
        if nearby_standalone:
            nearby_standalone.sort(key=lambda row: (row[0], row[1]))
            return nearby_standalone[0][1], self.COORDINATION_MODE_STANDALONE
        return fallback_map_id, self.COORDINATION_MODE_STANDALONE

    async def _resolve_persisted_mapped_coordination_map_id(
            self,
            organization_id: str,
            zone_id: str,
            lat: float,
            lng: float,
            now_ms: int) -> Optional[str]:
        if not self._has_usable_location(lat, lng):
            return None
        recent_cutoff_ms = now_ms - (R2C_HEARTBEAT_SEC * 1000 * 2)
        candidates: list[tuple[float, str]] = []
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CZoneState).where(
                    R2CZoneState.organization_id == organization_id,
                    R2CZoneState.last_seen_ms >= recent_cutoff_ms,
                    R2CZoneState.map_id.not_like(f"{self.STANDALONE_PREFIX}%"),
                )
            )
            for zone in result.scalars().all():
                if zone.zone_id == zone_id:
                    continue
                if getattr(zone, "coordination_mode", self.COORDINATION_MODE_MAP) != self.COORDINATION_MODE_MAP:
                    continue
                if not self._has_usable_location(zone.lat, zone.lng):
                    continue
                distance_m = self._distance_meters(lat, lng, zone.lat, zone.lng)
                if distance_m <= self.STANDALONE_GROUP_RADIUS_M:
                    candidates.append((distance_m, zone.map_id))
        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[0], row[1]))
        return candidates[0][1]

    def _message_context(self, websocket: WebSocket, payload: dict) -> tuple[str, str, str, str]:
        conn = self._connections.get(websocket)
        payload_zone_id = payload.get("zoneId", "") or payload.get("guid", "")
        if conn is not None:
            return (
                conn.organization_id,
                conn.map_id or payload.get("mapId", ""),
                conn.zone_id or payload_zone_id,
                conn.guid or payload.get("guid", payload_zone_id),
            )
        return (
            LEGACY_COORDINATION_ORGANIZATION_ID,
            payload.get("mapId", ""),
            payload_zone_id,
            payload.get("guid", payload_zone_id),
        )

    def _prune_confirmed_drones_locked(self, now_ms: int):
        cutoff_ms = now_ms - self.CONFIRMATION_RETENTION_MS
        for scope_key in list(self._confirmed_drones_by_map.keys()):
            confirmations = self._confirmed_drones_by_map.get(scope_key, {})
            for remote_id in list(confirmations.keys()):
                if int(confirmations[remote_id].get("confirmedAtMs", 0) or 0) < cutoff_ms:
                    confirmations.pop(remote_id, None)
            if not confirmations:
                self._confirmed_drones_by_map.pop(scope_key, None)

    def _remember_drone_confirmation_locked(self, organization_id: str, map_id: str, event: dict, now_ms: int):
        if not map_id or not event.get("remoteId"):
            return
        self._prune_confirmed_drones_locked(now_ms)
        stored = dict(event)
        stored["mapId"] = map_id
        stored["confirmedAtMs"] = now_ms
        scope_key = self._scope_key(organization_id, map_id)
        self._confirmed_drones_by_map.setdefault(scope_key, {})[str(event["remoteId"])] = stored

    def _merge_drone_confirmations_locked(self, organization_id: str, old_map_id: Optional[str], new_map_id: str, now_ms: int) -> list[dict]:
        if not old_map_id or not new_map_id or old_map_id == new_map_id:
            return []
        self._prune_confirmed_drones_locked(now_ms)
        old_scope_key = self._scope_key(organization_id, old_map_id)
        new_scope_key = self._scope_key(organization_id, new_map_id)
        old_confirmations = self._confirmed_drones_by_map.get(old_scope_key, {})
        if not old_confirmations:
            return []
        new_confirmations = self._confirmed_drones_by_map.setdefault(new_scope_key, {})
        merged: list[dict] = []
        for remote_id, event in old_confirmations.items():
            copied = dict(event)
            copied["mapId"] = new_map_id
            existing = new_confirmations.get(remote_id)
            if existing is None or int(copied.get("confirmedAtMs", 0) or 0) >= int(existing.get("confirmedAtMs", 0) or 0):
                new_confirmations[remote_id] = copied
                merged.append(copied)
        return merged

    def _recent_drone_confirmations_locked(self, organization_id: str, map_id: str, now_ms: int) -> list[dict]:
        self._prune_confirmed_drones_locked(now_ms)
        return [
            dict(event)
            for event in self._confirmed_drones_by_map.get(self._scope_key(organization_id, map_id), {}).values()
        ]

    def _forget_drone_confirmation_locked(self, organization_id: str, map_id: str, remote_id: str):
        if not map_id or not remote_id:
            return
        scope_key = self._scope_key(organization_id, map_id)
        confirmations = self._confirmed_drones_by_map.get(scope_key, {})
        confirmations.pop(remote_id, None)
        if not confirmations:
            self._confirmed_drones_by_map.pop(scope_key, None)

    def _forget_drone_confirmations_for_zone_locked(self, organization_id: str, map_id: str, guid: str, zone_id: str):
        if not map_id:
            return
        scope_key = self._scope_key(organization_id, map_id)
        confirmations = self._confirmed_drones_by_map.get(scope_key, {})
        for remote_id, event in list(confirmations.items()):
            confirmed_by_guid = str(event.get("confirmedByGuid", "") or event.get("guid", "") or "")
            event_zone_id = str(event.get("zoneId", "") or "")
            if (guid and confirmed_by_guid == guid) or (zone_id and event_zone_id == zone_id):
                confirmations.pop(remote_id, None)
        if not confirmations:
            self._confirmed_drones_by_map.pop(scope_key, None)

    def _dedupe_confirmation_events(self, events: list[dict]) -> list[dict]:
        by_remote_id: dict[str, dict] = {}
        for event in events:
            remote_id = str(event.get("remoteId", "") or "")
            if not remote_id:
                continue
            existing = by_remote_id.get(remote_id)
            if existing is None or int(event.get("confirmedAtMs", 0) or 0) >= int(existing.get("confirmedAtMs", 0) or 0):
                by_remote_id[remote_id] = event
        return list(by_remote_id.values())

    async def _send_drone_confirmation_to_zone(self, zone: R2CZoneConnection, event: dict):
        remote_id = str(event.get("remoteId", "") or "")
        event_key = f"{remote_id}:{event.get('confirmationEventId') or int(event.get('confirmedAtMs', 0) or 0)}"
        if not remote_id or event_key in zone.sent_confirmed_event_keys or zone.websocket is None:
            return
        try:
            await zone.websocket.send_text(json.dumps(event))
            zone.sent_confirmed_event_keys.add(event_key)
        except Exception as e:
            logger.warning("drone_confirmed send failed for %s/%s: %s", event.get("mapId", ""), zone.zone_id, e)

    async def _broadcast_drone_confirmation(self, organization_id: str, map_id: str, event: dict):
        async with self._lock:
            recipients = [zone for zone in self._zones_by_map.get(self._scope_key(organization_id, map_id), {}).values() if zone.websocket is not None]
        for zone in recipients:
            await self._send_drone_confirmation_to_zone(zone, event)

    async def _send_recent_drone_confirmations(self, websocket: WebSocket, organization_id: str, map_id: str, now_ms: int):
        async with self._lock:
            zone = self._connections.get(websocket)
            events = self._recent_drone_confirmations_locked(organization_id, map_id, now_ms)
        events.extend(await self._load_recent_confirmation_events(organization_id, map_id, now_ms))
        events = self._dedupe_confirmation_events(events)
        if zone is None:
            return
        for event in events:
            await self._send_drone_confirmation_to_zone(zone, event)

    async def connect(
        self,
        websocket: WebSocket,
        device_credential: Optional[object] = None,
    ):
        await websocket.accept()
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        organization_id = str(
            getattr(device_credential, "organization_id", "")
            or LEGACY_COORDINATION_ORGANIZATION_ID
        )
        async with self._lock:
            conn = R2CZoneConnection(websocket, device_credential, organization_id)
            conn.connected_at_ms = now_ms
            self._connections[websocket] = conn
            if device_credential is not None:
                self._connections_by_device_credential_id[
                    device_credential.id
                ] = conn

    def _device_connections_locked(
        self,
        device_credential_id: str,
    ) -> list[R2CZoneConnection]:
        """Return every live local connection for an organization device."""
        preferred = self._connections_by_device_credential_id.get(
            device_credential_id
        )
        matches = []
        if preferred is not None and preferred.websocket in self._connections:
            matches.append(preferred)
        for connection in self._connections.values():
            credential = connection.device_credential
            if (
                credential is not None
                and credential.id == device_credential_id
                and connection not in matches
            ):
                matches.append(connection)
        matches.sort(
            key=lambda connection: (
                int(getattr(connection, "last_seen_ms", 0) or 0),
                int(getattr(connection, "connected_at_ms", 0) or 0),
            ),
            reverse=True,
        )
        return matches

    async def disconnect_device_credential(
        self,
        device_credential_id: str,
        *,
        reason: str,
        reauthentication_url: str = "",
    ) -> None:
        async with self._lock:
            connections = list(
                self._device_connections_locked(device_credential_id)
            )
        for connection in connections:
            try:
                await connection.websocket.send_json({
                    "type": "reauthentication_required",
                    "clearManagedConfiguration": False,
                    "reauthenticationUrl": reauthentication_url,
                    "message": reason,
                })
                await connection.websocket.close(code=1008, reason=reason)
            except Exception as exc:
                logger.info(
                    "Could not notify device %s before disconnect: %s",
                    device_credential_id,
                    exc,
                )

    async def remote_video_control_enabled(
        self,
        *,
        organization_id: str,
        device_credential_id: str = "",
    ) -> bool:
        """Return the latest live Remote Video Control setting in scope."""
        async with self._lock:
            connections = (
                self._device_connections_locked(device_credential_id)
                if device_credential_id
                else list(self._connections.values())
            )
            return any(
                connection.organization_id == organization_id
                and connection.remote_video_control_enabled
                for connection in connections
            )

    async def disconnect(self, websocket: WebSocket):
        confirmed_owner_expirations: list[tuple[str, str, str, str]] = []
        async with self._lock:
            conn = self._connections.pop(websocket, None)
            if conn is None:
                return
            if (
                conn.device_credential is not None
                and self._connections_by_device_credential_id.get(
                    conn.device_credential.id
                ) is conn
            ):
                self._connections_by_device_credential_id.pop(
                    conn.device_credential.id,
                    None,
                )
                replacements = self._device_connections_locked(
                    conn.device_credential.id
                )
                if replacements:
                    self._connections_by_device_credential_id[
                        conn.device_credential.id
                    ] = replacements[0]
            map_id = conn.map_id
            organization_id = conn.organization_id
            scope_key = self._scope_key(organization_id, map_id or "")
            zone_guid = conn.guid or ""
            zone_id = conn.zone_id or ""
            name = conn.name
            lat = conn.lat
            lng = conn.lng
            caltopo_rtt_ms = conn.caltopo_rtt_ms
            app_version = conn.app_version
            app_version_code = conn.app_version_code
            connection_state = "idle" if conn.connection_state == "idle" else "disconnected"
            connected_at_ms = conn.connected_at_ms
            hello_received_at_ms = conn.hello_received_at_ms
            last_seen_ms = conn.last_seen_ms
            should_mark_zone_offline = False
            if map_id and zone_id:
                zones = self._zones_by_map.get(scope_key, {})
                tracked = zones.get(zone_id)
                if tracked is conn:
                    conn.websocket = None
                    should_mark_zone_offline = True
            if should_mark_zone_offline:
                self._forget_drone_confirmations_for_zone_locked(organization_id, map_id, zone_guid, zone_id)
                for (owner_organization_id, owner_map_id, remote_id), owner in list(self._owners.items()):
                    if owner_organization_id != organization_id or owner_map_id != map_id or owner.get("source") != "confirmation":
                        continue
                    owner_guid = str(owner.get("owner_guid", "") or "")
                    owner_zone_id = str(owner.get("owner_zone_id", "") or "")
                    if (zone_guid and owner_guid == zone_guid) or (zone_id and owner_zone_id == zone_id):
                        self._owners.pop((owner_organization_id, owner_map_id, remote_id), None)
                        self._forget_drone_confirmation_locked(owner_organization_id, owner_map_id, remote_id)
                        confirmed_owner_expirations.append((owner_organization_id, owner_map_id, remote_id, owner_guid))
            reported_map_id = conn.reported_map_id
            coordination_mode = conn.coordination_mode
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        if map_id and zone_id and should_mark_zone_offline:
            logger.info(
                "r2c websocket disconnected: map=%s zone=%s guid=%s conn_age_ms=%s hello_age_ms=%s last_seen_age_ms=%s",
                map_id,
                zone_id,
                zone_guid,
                max(now_ms - int(connected_at_ms or 0), 0),
                max(now_ms - int(hello_received_at_ms or connected_at_ms or 0), 0),
                max(now_ms - int(last_seen_ms or 0), 0),
            )
            await self._upsert_zone_state(
                organization_id,
                map_id,
                zone_id,
                zone_guid or zone_id,
                name,
                lat,
                lng,
                caltopo_rtt_ms,
                False,
                last_seen_ms,
                reported_map_id,
                coordination_mode,
                connection_state,
                app_version,
                app_version_code,
            )
            await self._delete_confirmation_state_for_zone(organization_id, map_id, zone_guid, zone_id)
        for owner_organization_id, owner_map_id, remote_id, owner_guid in confirmed_owner_expirations:
            logger.info(
                "r2c owner_expired: map=%s remote_id=%s reason=confirming_zone_disconnect prev_owner_guid=%s",
                owner_map_id,
                remote_id,
                owner_guid,
            )
            await self._delete_owner_state(owner_organization_id, owner_map_id, remote_id)
            await self._delete_confirmation_state(owner_organization_id, owner_map_id, remote_id)
            await self.broadcast(
                owner_organization_id,
                owner_map_id,
                {
                    "type": "owner_expired",
                    "remoteId": remote_id,
                    "prevOwnerGuid": owner_guid
                }
            )
        if map_id:
            await self.broadcast_zone_update(organization_id, map_id)

    async def handle_message(self, websocket: WebSocket, payload: dict):
        mtype = payload.get("type", "")
        if mtype == "hello":
            await self._handle_hello(websocket, payload)
        elif mtype == "heartbeat":
            await self._handle_heartbeat(websocket, payload)
        elif mtype == "first_sighting":
            await self._handle_first_sighting(websocket, payload)
        elif mtype == "sighting":
            await self._handle_sighting(websocket, payload)
        elif mtype == "drone_lost":
            await self._handle_drone_lost(websocket, payload)
        elif mtype == "drone_confirmed":
            await self._handle_drone_confirmed(websocket, payload)
        elif mtype == "idle":
            await self._handle_idle(websocket, payload)
        elif mtype == "video_stream_advertisement":
            await self._handle_video_stream_advertisement(websocket, payload)
        elif mtype == "video_preflight_result":
            await self._handle_video_preflight_result(websocket, payload)
        elif mtype == "video_preflight_answer":
            await self._handle_video_preflight_answer(websocket, payload)
        elif mtype == "video_stream_decision":
            await self._handle_video_stream_decision(websocket, payload)
        elif mtype == "video_stream_unavailable":
            await self._handle_video_stream_unavailable(websocket, payload)
        elif mtype == "video_media_answer":
            await self._handle_video_media_answer(websocket, payload)
        elif mtype == "video_stream_terminated":
            await self._handle_video_stream_terminated(websocket, payload)
        elif mtype == "recording_download_decision":
            await self._handle_recording_download_decision(websocket, payload)
        elif mtype == "organization_config_snapshot_response":
            await self._handle_organization_config_snapshot_response(websocket, payload)

    async def _handle_organization_config_snapshot_response(
        self, websocket: WebSocket, payload: dict,
    ):
        async with self._lock:
            connection = self._connections.get(websocket)
            credential = connection.device_credential if connection is not None else None
        request_id = str(payload.get("requestId", "") or "").strip()
        try:
            if credential is None or control_plane_store is None:
                raise ControlPlaneError("Organization device credential required.")
            _snapshot, snapshot_json = validated_organization_config_snapshot(
                payload.get("config")
            )
            current = await control_plane_store.get_current_organization_config_release(
                credential.organization_id
            )
            diff = organization_config_diff(
                current.snapshot if current is not None else None,
                _snapshot,
            )
            await control_plane_store.complete_organization_config_proposal(
                proposal_id=request_id,
                organization_id=credential.organization_id,
                device_credential_id=credential.id,
                snapshot_json=snapshot_json,
                diff_json=json.dumps(diff, separators=(",", ":"), sort_keys=True),
            )
            await websocket.send_text(json.dumps({
                "type": "organization_config_snapshot_ack",
                "requestId": request_id,
                "accepted": True,
            }))
        except (ControlPlaneError, ValueError) as exc:
            logger.warning("Organization config response rejected: request=%s error=%s", request_id, exc)
            await websocket.send_text(json.dumps({
                "type": "organization_config_snapshot_ack",
                "requestId": request_id,
                "accepted": False,
                "error": str(exc),
            }))

    async def _handle_recording_download_decision(self, websocket: WebSocket, payload: dict):
        async with self._lock:
            connection = self._connections.get(websocket)
            credential = connection.device_credential if connection is not None else None
        request_id = str(payload.get("requestId", "") or "").strip()
        try:
            if credential is None or control_plane_store is None:
                raise ControlPlaneError("Organization device credential required.")
            result = await control_plane_store.decide_recording_download_request(
                request_id=request_id,
                device_credential_id=credential.id,
                approved=str(payload.get("decision", "")).lower() == "approve",
            )
            logger.info(
                "Recording transfer decision accepted: request=%s device=%s state=%s",
                result.id,
                credential.id,
                result.state,
            )
            await websocket.send_text(json.dumps({
                "type": "recording_download_decision_ack", "requestId": result.id,
                "accepted": True, "state": result.state,
            }))
        except ControlPlaneError as exc:
            logger.warning(
                "Recording transfer decision rejected: request=%s error=%s",
                request_id,
                exc,
            )
            await websocket.send_text(json.dumps({
                "type": "recording_download_decision_ack", "requestId": request_id,
                "accepted": False, "error": str(exc),
            }))

    async def _handle_video_stream_advertisement(
        self,
        websocket: WebSocket,
        payload: dict,
    ):
        remote_control_enabled = bool(
            payload.get("remoteControlEnabled", False)
        )
        async with self._lock:
            conn = self._connections.get(websocket)
            credential = conn.device_credential if conn is not None else None
            first_inventory_after_connect = bool(
                conn is not None and not conn.video_inventory_reconciled
            )
            if conn is not None:
                conn.remote_video_control_enabled = remote_control_enabled
                conn.video_inventory_reconciled = True
        if credential is None or control_plane_store is None:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "video_stream_advertisement_ack",
                        "accepted": False,
                        "error": "Organization device credential required",
                    }
                )
            )
            return
        incident_name = str(payload.get("incidentName", "") or "").strip()
        timezone_name = str(payload.get("timeZone", "UTC") or "UTC").strip()
        advertised_streams = payload.get("streams", [])
        if not isinstance(advertised_streams, list):
            advertised_streams = []
        accepted_session_ids: list[str] = []
        for advertised in advertised_streams[:24]:
            if not isinstance(advertised, dict):
                continue
            try:
                stream = await control_plane_store.advertise_video_stream(
                    organization_id=credential.organization_id,
                    device_credential_id=credential.id,
                    device_name=str(payload.get("deviceName", "") or ""),
                    session_id=str(advertised.get("sessionId", "") or ""),
                    incident_name=incident_name,
                    drone_designator=str(
                        advertised.get("droneDesignator", "") or ""
                    ),
                    source_width=int(advertised.get("sourceWidth", 0) or 0),
                    source_height=int(advertised.get("sourceHeight", 0) or 0),
                    source_fps=float(advertised.get("sourceFps", 0.0) or 0.0),
                    source_bitrate_bps=int(
                        advertised.get("sourceBitrateBps", 0) or 0
                    ),
                    source_codec=str(advertised.get("sourceCodec", "") or ""),
                    media_kind=str(advertised.get("mediaKind", "live") or "live"),
                    recorded_at=optional_iso_datetime(
                        advertised.get("recordedAt")
                    ),
                    duration_ms=int(advertised.get("durationMs", 0) or 0),
                    thumbnail_revision=str(
                        advertised.get("thumbnailRevision", "") or ""
                    ),
                    timezone_name=timezone_name,
                    remote_control_enabled=remote_control_enabled,
                )
                accepted_session_ids.append(stream.session_id)
                await self.cache_video_thumbnail(
                    device_credential_id=credential.id,
                    session_id=stream.session_id,
                    revision=stream.thumbnail_revision,
                    jpeg_base64=str(
                        advertised.get("thumbnailJpegBase64", "") or ""
                    ),
                )
            except (ControlPlaneError, TypeError, ValueError):
                logger.warning(
                    "Rejected managed stream advertisement from device=%s",
                    credential.id,
                    exc_info=True,
                )
        await control_plane_store.reconcile_device_video_streams(
            organization_id=credential.organization_id,
            device_credential_id=credential.id,
            active_session_ids=accepted_session_ids,
            notify_even_if_unchanged=first_inventory_after_connect,
        )
        if first_inventory_after_connect:
            # A reconnect is an authoritative, event-driven repair boundary.
            # Wake focused viewers once even if the prior 45-second presence
            # lease made the accepted session set look unchanged.
            await organization_stream_event_hub.broadcast(
                credential.organization_id
            )
        pending_requests = await self._pending_video_stream_requests(
            credential.id
        )
        pending_preflight_offers = await self._pending_video_preflight_offers(
            credential.id
        )
        pending_media_offers = await self._pending_video_media_offers(
            credential.id
        )
        pending_recording_downloads = (
            await self._pending_recording_download_requests(credential.id)
            if first_inventory_after_connect
            else ()
        )
        await websocket.send_text(
            json.dumps(
                {
                    "type": "video_stream_advertisement_ack",
                    "accepted": True,
                    "sessionIds": accepted_session_ids,
                    "presenceTtlSec": 45,
                }
            )
        )
        for stream_request in pending_requests:
            await self.send_video_stream_request(
                device_credential_id=stream_request.device_credential_id,
                request_id=stream_request.id,
                requester_email=stream_request.requester_email,
                stream_session_id=stream_request.stream_session_id,
                incident_name=stream_request.incident_name,
                drone_designator=stream_request.drone_designator,
                source_width=stream_request.source_width,
                source_height=stream_request.source_height,
                source_fps=stream_request.source_fps,
                source_bitrate_bps=stream_request.source_bitrate_bps,
                source_codec=stream_request.source_codec,
                remote_control_enabled=stream_request.remote_control_enabled,
                expires_at=stream_request.expires_at,
            )
        for exchange in pending_preflight_offers:
            await self.send_video_preflight_offer(exchange)
        for exchange in pending_media_offers:
            await self.send_video_media_offer(exchange)
        for recording_download in pending_recording_downloads:
            await self.send_recording_download_request(recording_download)

    async def _pending_video_stream_requests(
        self,
        device_credential_id: str,
    ) -> tuple:
        if control_plane_store is None:
            return ()
        try:
            return (
                await control_plane_store
                .list_pending_video_stream_requests_for_device(
                    device_credential_id=device_credential_id,
                )
            )
        except Exception:
            logger.warning(
                "Managed video request replay lookup failed for device=%s",
                device_credential_id,
                exc_info=True,
            )
            return ()

    async def _handle_video_preflight_result(
        self,
        websocket: WebSocket,
        payload: dict,
    ):
        async with self._lock:
            connection = self._connections.get(websocket)
            credential = (
                connection.device_credential
                if connection is not None
                else None
            )
        request_id = str(payload.get("requestId", "") or "").strip()
        try:
            if credential is None or control_plane_store is None:
                raise ControlPlaneError(
                    "Organization device credential required."
                )
            result = await control_plane_store.record_video_preflight_result(
                request_id=request_id,
                device_credential_id=credential.id,
                route_kind=str(payload.get("routeKind", "") or ""),
                estimated_uplink_bps=int(
                    payload.get("estimatedUplinkBps", 0) or 0
                ),
            )
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "video_preflight_result_ack",
                        "requestId": result.id,
                        "accepted": True,
                        "state": result.state,
                    }
                )
            )
        except (ControlPlaneError, TypeError, ValueError) as exc:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "video_preflight_result_ack",
                        "requestId": request_id,
                        "accepted": False,
                        "error": str(exc),
                    }
                )
            )

    async def _handle_video_preflight_answer(
        self,
        websocket: WebSocket,
        payload: dict,
    ):
        async with self._lock:
            connection = self._connections.get(websocket)
            credential = (
                connection.device_credential
                if connection is not None
                else None
            )
        request_id = str(payload.get("requestId", "") or "").strip()
        try:
            if credential is None or control_plane_store is None:
                raise ControlPlaneError(
                    "Organization device credential required."
                )
            result = await control_plane_store.record_video_preflight_answer(
                request_id=request_id,
                device_credential_id=credential.id,
                device_answer_sdp=str(payload.get("sdp", "") or ""),
            )
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "video_preflight_answer_ack",
                        "requestId": result.request_id,
                        "accepted": True,
                    }
                )
            )
        except (ControlPlaneError, TypeError, ValueError) as exc:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "video_preflight_answer_ack",
                        "requestId": request_id,
                        "accepted": False,
                        "error": str(exc),
                    }
                )
            )

    async def _handle_video_stream_decision(
        self,
        websocket: WebSocket,
        payload: dict,
    ):
        async with self._lock:
            connection = self._connections.get(websocket)
            credential = (
                connection.device_credential
                if connection is not None
                else None
            )
        request_id = str(payload.get("requestId", "") or "").strip()
        try:
            if credential is None or control_plane_store is None:
                raise ControlPlaneError(
                    "Organization device credential required."
                )
            result = await control_plane_store.record_video_stream_decision(
                request_id=request_id,
                device_credential_id=credential.id,
                decision=str(payload.get("decision", "") or ""),
                selected_width=int(payload.get("selectedWidth", 0) or 0),
                selected_height=int(payload.get("selectedHeight", 0) or 0),
                selected_fps=float(payload.get("selectedFps", 0.0) or 0.0),
                selected_bitrate_bps=int(
                    payload.get("selectedBitrateBps", 0) or 0
                ),
            )
            await websocket.send_text(json.dumps({
                "type": "video_stream_decision_ack",
                "requestId": result.id,
                "accepted": True,
                "state": result.state,
            }))
            logger.info(
                "Managed video decision accepted: request=%s device=%s "
                "state=%s profile=%sx%s@%s bitrate=%s",
                result.id,
                credential.id,
                result.state,
                getattr(result, "selected_width", 0),
                getattr(result, "selected_height", 0),
                getattr(result, "selected_fps", 0.0),
                getattr(result, "selected_bitrate_bps", 0),
            )
        except (ControlPlaneError, TypeError, ValueError) as exc:
            logger.warning(
                "Managed video decision rejected: request=%s device=%s error=%s",
                request_id,
                getattr(credential, "id", ""),
                exc,
            )
            await websocket.send_text(json.dumps({
                "type": "video_stream_decision_ack",
                "requestId": request_id,
                "accepted": False,
                "error": str(exc),
            }))

    async def _handle_video_stream_unavailable(
        self,
        websocket: WebSocket,
        payload: dict,
    ):
        async with self._lock:
            connection = self._connections.get(websocket)
            credential = (
                connection.device_credential
                if connection is not None
                else None
            )
        request_id = str(payload.get("requestId", "") or "").strip()
        try:
            if credential is None or control_plane_store is None:
                raise ControlPlaneError(
                    "Organization device credential required."
                )
            result = await control_plane_store.record_video_stream_unavailable(
                request_id=request_id,
                device_credential_id=credential.id,
                stream_session_id=str(
                    payload.get("streamSessionId", "") or ""
                ),
                error_code=str(
                    payload.get("errorCode", "e_nosuch_stream")
                    or "e_nosuch_stream"
                ),
            )
            await websocket.send_text(json.dumps({
                "type": "video_stream_unavailable_ack",
                "requestId": result.id,
                "accepted": True,
                "state": result.state,
            }))
        except (ControlPlaneError, TypeError, ValueError) as exc:
            await websocket.send_text(json.dumps({
                "type": "video_stream_unavailable_ack",
                "requestId": request_id,
                "accepted": False,
                "error": str(exc),
            }))

    async def _handle_video_media_answer(self, websocket: WebSocket, payload: dict):
        async with self._lock:
            connection = self._connections.get(websocket)
            credential = connection.device_credential if connection is not None else None
        request_id = str(payload.get("requestId", "") or "").strip()
        try:
            if credential is None or control_plane_store is None:
                raise ControlPlaneError("Organization device credential required.")
            result = await control_plane_store.record_video_media_answer(
                request_id=request_id,
                device_credential_id=credential.id,
                device_answer_sdp=str(payload.get("sdp", "") or ""),
            )
            await websocket.send_text(json.dumps({
                "type": "video_media_answer_ack",
                "requestId": result.request_id,
                "accepted": True,
            }))
            logger.info(
                "Managed video media answer accepted: request=%s device=%s sdpBytes=%s",
                result.request_id,
                credential.id,
                len(str(payload.get("sdp", "") or "").encode("utf-8")),
            )
        except (ControlPlaneError, TypeError, ValueError) as exc:
            logger.warning(
                "Managed video media answer rejected: request=%s device=%s error=%s",
                request_id,
                getattr(credential, "id", ""),
                exc,
            )
            await websocket.send_text(json.dumps({
                "type": "video_media_answer_ack",
                "requestId": request_id,
                "accepted": False,
                "error": str(exc),
            }))

    async def _handle_video_stream_terminated(self, websocket: WebSocket, payload: dict):
        async with self._lock:
            connection = self._connections.get(websocket)
            credential = connection.device_credential if connection is not None else None
        request_id = str(payload.get("requestId", "") or "").strip()
        try:
            if credential is None or control_plane_store is None:
                raise ControlPlaneError("Organization device credential required.")
            result = await control_plane_store.stop_video_stream_from_device(
                request_id=request_id,
                device_credential_id=credential.id,
                reason=str(payload.get("reason", "device_terminated") or "device_terminated"),
            )
            await websocket.send_text(json.dumps({
                "type": "video_stream_terminated_ack",
                "requestId": result.id,
                "accepted": True,
                "state": result.state,
            }))
        except (ControlPlaneError, TypeError, ValueError) as exc:
            await websocket.send_text(json.dumps({
                "type": "video_stream_terminated_ack",
                "requestId": request_id,
                "accepted": False,
                "error": str(exc),
            }))

    async def _pending_video_preflight_offers(
        self,
        device_credential_id: str,
    ) -> tuple:
        if control_plane_store is None:
            return ()
        try:
            return (
                await control_plane_store
                .list_pending_video_preflight_offers_for_device(
                    device_credential_id=device_credential_id,
                )
            )
        except Exception:
            logger.warning(
                "Managed video preflight replay lookup failed for device=%s",
                device_credential_id,
                exc_info=True,
            )
            return ()

    async def _pending_video_media_offers(self, device_credential_id: str) -> tuple:
        if control_plane_store is None:
            return ()
        try:
            return await control_plane_store.list_pending_video_media_offers_for_device(
                device_credential_id=device_credential_id
            )
        except Exception:
            logger.warning(
                "Managed video media replay lookup failed for device=%s",
                device_credential_id,
                exc_info=True,
            )
            return ()

    async def _pending_recording_download_requests(
        self,
        device_credential_id: str,
    ) -> tuple:
        if control_plane_store is None:
            return ()
        try:
            return await control_plane_store.list_pending_recording_download_requests_for_device(
                device_credential_id=device_credential_id
            )
        except Exception:
            logger.warning(
                "Recording download replay lookup failed for device=%s",
                device_credential_id,
                exc_info=True,
            )
            return ()

    async def send_video_preflight_offer(self, exchange) -> bool:
        ice_servers = await video_ice_server_provider.get_ice_servers(
            f"organization:{exchange.organization_id}"
        )
        async with self._lock:
            connections = self._device_connections_locked(
                exchange.device_credential_id
            )
            connection = connections[0] if connections else None
            websocket = connection.websocket if connection is not None else None
        if websocket is None:
            logger.info(
                "Managed video preflight offer pending: request=%s device=%s websocket=offline",
                exchange.request_id,
                exchange.device_credential_id,
            )
            return False
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "video_preflight_offer",
                        "requestId": exchange.request_id,
                        "sdp": exchange.browser_offer_sdp,
                        "iceServers": ice_servers,
                        "expiresAt": exchange.expires_at.isoformat(),
                        "probeDurationMs": 2000,
                    }
                )
            )
            logger.info(
                "Managed video preflight offer delivered: request=%s device=%s",
                exchange.request_id,
                exchange.device_credential_id,
            )
            return True
        except Exception:
            logger.warning(
                "Managed video preflight offer delivery failed",
                exc_info=True,
            )
            return False

    async def send_video_thumbnail_preview(
        self,
        *,
        device_credential_id: str,
        ttl_seconds: int = 25,
    ) -> bool:
        """Renew a bounded thumbnail-preview lease on one connected tablet."""
        safe_ttl = max(10, min(int(ttl_seconds), 60))
        async with self._lock:
            connections = self._device_connections_locked(device_credential_id)
            connection = connections[0] if connections else None
            websocket = connection.websocket if connection is not None else None
        if websocket is None:
            return False
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "video_thumbnail_preview",
                        "ttlSec": safe_ttl,
                    }
                )
            )
            return True
        except Exception:
            logger.warning(
                "Managed thumbnail preview delivery failed for device=%s",
                device_credential_id,
                exc_info=True,
            )
            return False

    async def send_video_media_offer(self, exchange) -> bool:
        ice_servers = await video_ice_server_provider.get_ice_servers(
            f"organization:{exchange.organization_id}"
        )
        async with self._lock:
            connections = self._device_connections_locked(
                exchange.device_credential_id
            )
            connection = connections[0] if connections else None
            websocket = connection.websocket if connection is not None else None
        if websocket is None:
            logger.info(
                "Managed video media offer pending: request=%s device=%s websocket=offline",
                exchange.request_id,
                exchange.device_credential_id,
            )
            return False
        try:
            await websocket.send_text(json.dumps({
                "type": "video_media_offer",
                "requestId": exchange.request_id,
                "streamSessionId": exchange.stream_session_id,
                "requesterEmail": getattr(exchange, "requester_email", ""),
                "routeKind": getattr(exchange, "route_kind", "unknown"),
                "selectedWidth": getattr(exchange, "selected_width", 0),
                "selectedHeight": getattr(exchange, "selected_height", 0),
                "selectedFps": getattr(exchange, "selected_fps", 0.0),
                "selectedBitrateBps": getattr(
                    exchange, "selected_bitrate_bps", 0
                ),
                "sdp": exchange.browser_offer_sdp,
                "iceServers": ice_servers,
                "expiresAt": exchange.expires_at.isoformat(),
            }))
            logger.info(
                "Managed video media offer delivered: request=%s device=%s",
                exchange.request_id,
                exchange.device_credential_id,
            )
            return True
        except Exception:
            logger.warning("Managed video media offer delivery failed", exc_info=True)
            return False

    async def send_video_stream_request(
        self,
        *,
        device_credential_id: str,
        request_id: str,
        requester_email: str,
        stream_session_id: str,
        incident_name: str,
        drone_designator: str,
        source_width: int,
        source_height: int,
        source_fps: float,
        source_bitrate_bps: int,
        source_codec: str,
        remote_control_enabled: bool,
        expires_at: datetime,
    ) -> bool:
        async with self._lock:
            connections = self._device_connections_locked(
                device_credential_id
            )
            connection = connections[0] if connections else None
            websocket = connection.websocket if connection is not None else None
        if websocket is None:
            return False
        try:
            await websocket.send_text(json.dumps({
                "type": "video_stream_request",
                "requestId": request_id,
                "requesterEmail": requester_email,
                "streamSessionId": stream_session_id,
                "incidentName": incident_name,
                "droneDesignator": drone_designator,
                "sourceWidth": source_width,
                "sourceHeight": source_height,
                "sourceFps": source_fps,
                "sourceBitrateBps": source_bitrate_bps,
                "sourceCodec": source_codec,
                "expiresAt": expires_at.isoformat(),
                "consentRequired": not remote_control_enabled,
                "remoteControlEnabled": remote_control_enabled,
            }))
            return True
        except Exception:
            logger.warning(
                "Managed video request delivery failed for device=%s",
                device_credential_id,
                exc_info=True,
            )
            return False

    async def send_organization_config_snapshot_request(
        self, *, device_credential_id: str, request_id: str,
    ) -> bool:
        async with self._lock:
            connections = self._device_connections_locked(device_credential_id)
            connection = connections[0] if connections else None
            websocket = connection.websocket if connection is not None else None
        if websocket is None:
            return False
        try:
            await websocket.send_text(json.dumps({
                "type": "organization_config_snapshot_request",
                "requestId": request_id,
            }))
            return True
        except Exception:
            logger.warning(
                "Organization config request delivery failed for device=%s",
                device_credential_id,
                exc_info=True,
            )
            return False

    async def send_recording_download_request(self, item) -> bool:
        async with self._lock:
            connections = self._device_connections_locked(item.device_credential_id)
            websocket = connections[0].websocket if connections else None
        if websocket is None:
            return False
        try:
            await websocket.send_text(json.dumps({
                "type": "recording_download_request",
                "requestId": item.id,
                "requesterEmail": item.requester_email,
                "streamSessionId": item.stream_session_id,
                "droneDesignator": item.drone_designator,
                "uploadPath": f"/recording-downloads/{item.id}/content",
                "expiresAt": item.expires_at.isoformat(),
                "consentRequired": (
                    item.state == "awaiting_approval"
                    and not item.remote_control_enabled
                ),
                "remoteControlEnabled": item.remote_control_enabled,
            }))
            return True
        except Exception:
            logger.warning("Recording download request delivery failed", exc_info=True)
            return False

    async def send_video_stream_request_cancelled(
        self,
        *,
        device_credential_id: str,
        request_id: str,
    ) -> bool:
        async with self._lock:
            connections = self._device_connections_locked(
                device_credential_id
            )
            connection = connections[0] if connections else None
            websocket = connection.websocket if connection is not None else None
        if websocket is None:
            return False
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "video_stream_request_cancelled",
                        "requestId": request_id,
                        "reason": "requester_cancelled",
                    }
                )
            )
            return True
        except Exception:
            logger.warning(
                "Managed video cancellation delivery failed for device=%s",
                device_credential_id,
                exc_info=True,
            )
            return False

    async def _handle_hello(self, websocket: WebSocket, payload: dict):
        reported_map_id = (payload.get("mapId", "") or "").strip()
        zone_id = payload.get("zoneId", "") or payload.get("guid", "")
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        async with self._lock:
            conn = self._connections[websocket]
            organization_id = conn.organization_id
            lat = float(payload.get("lat", 0.0) or 0.0)
            lng = float(payload.get("lng", 0.0) or 0.0)
            map_id, coordination_mode = self._resolve_coordination_map_id(
                organization_id,
                reported_map_id,
                zone_id,
                payload.get("guid", zone_id),
                lat,
                lng,
            )
            old_map_id = conn.map_id
            old_zone_id = conn.zone_id
            if old_map_id and old_zone_id:
                old_scope_key = self._scope_key(organization_id, old_map_id)
                old_zones = self._zones_by_map.get(old_scope_key, {})
                if old_zones.get(old_zone_id) is conn:
                    old_zones.pop(old_zone_id, None)
                if not old_zones:
                    self._zones_by_map.pop(old_scope_key, None)
            conn.map_id = map_id
            conn.reported_map_id = reported_map_id
            conn.coordination_mode = coordination_mode
            conn.zone_id = zone_id
            conn.guid = payload.get("guid", zone_id)
            reported_name = str(payload.get("name", zone_id) or zone_id)[:160]
            conn.name = reported_name
            if conn.device_credential is not None and reported_name.strip():
                # The marker code is computed from the tablet's current
                # operator-visible name, so live alias resolution must use the
                # name reported by this authenticated connection rather than a
                # possibly older enrollment label.
                conn.device_credential = type(conn.device_credential)(
                    **{
                        **vars(conn.device_credential),
                        "device_name": reported_name.strip(),
                    }
                )
            conn.app_version = str(payload.get("appVersion", "") or "")
            conn.app_version_code = self._parse_nonnegative_int(payload.get("appVersionCode"))
            conn.lat = lat
            conn.lng = lng
            conn.caltopo_rtt_ms = self._parse_caltopo_rtt_ms(payload.get("caltopoRttMs"))
            conn.connection_state = "online"
            conn.hello_received_at_ms = now_ms
            conn.last_seen_ms = now_ms
            zones = self._zones_by_map.setdefault(self._scope_key(organization_id, map_id), {})
            zones[zone_id] = conn
        logger.info(
            "r2c hello received: map=%s reported_map=%s coordination_mode=%s zone=%s guid=%s handshake_age_ms=%s",
            map_id,
            reported_map_id,
            coordination_mode,
            zone_id,
            conn.guid or zone_id,
            max(now_ms - int(conn.connected_at_ms or now_ms), 0),
        )
        await self._upsert_zone_state(
            organization_id,
            map_id,
            zone_id,
            conn.guid or zone_id,
            conn.name,
            conn.lat,
            conn.lng,
            conn.caltopo_rtt_ms,
            True,
            now_ms,
            reported_map_id,
            coordination_mode,
            "online",
            conn.app_version,
            conn.app_version_code,
        )
        hello_ack = {
            "type": "hello_ack",
            "serverTime": now_ms,
            "mapId": map_id,
            "heartbeatSec": R2C_HEARTBEAT_SEC,
            "leaseSec": R2C_LEASE_SEC,
            "idleRecommended": True,
            "idleParkSec": R2C_IDLE_PARK_SEC,
            "organizationConfigVersionMs": (
                await globals()["control_plane_store"].get_organization_config_version_ms(
                    organization_id
                )
                if conn.device_credential is not None and globals().get("control_plane_store") is not None
                else 0
            ),
        }
        app_platform = str(payload.get("appPlatform", "") or "").strip().lower()
        if app_platform in {"ios", "ipados"}:
            recommended_version_code = R2C_RECOMMENDED_IOS_APP_BUILD_NUMBER
            update_url = R2C_IOS_UPDATE_URL
        else:
            # Clients predating appPlatform are Android clients.
            recommended_version_code = R2C_RECOMMENDED_APP_VERSION_CODE
            update_url = R2C_UPDATE_URL
        if recommended_version_code > 0:
            hello_ack["recommendedAppVersionCode"] = recommended_version_code
        if update_url:
            hello_ack["updateUrl"] = update_url
        await websocket.send_text(json.dumps(hello_ack))
        await self.broadcast_zone_update(organization_id, map_id)
        await self._send_recent_drone_confirmations(websocket, organization_id, map_id, now_ms)

    async def _handle_idle(self, websocket: WebSocket, payload: dict):
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        async with self._lock:
            conn = self._connections.get(websocket)
            if conn is None:
                return
            organization_id, map_id, zone_id, guid = self._message_context(websocket, payload)
            if not map_id or not zone_id:
                return
            active_owner_remote_ids = [
                remote_id
                for (owner_organization_id, owner_map_id, remote_id), owner in self._owners.items()
                if owner_organization_id == organization_id
                and owner_map_id == map_id
                and int(owner.get("lease_expire_ms", 0) or 0) >= now_ms
                and (
                    (guid and owner.get("owner_guid") == guid)
                    or (zone_id and owner.get("owner_zone_id") == zone_id)
                )
            ]
            if active_owner_remote_ids:
                logger.warning(
                    "r2c zone_idle ignored while owner leases active: map=%s zone=%s guid=%s remote_ids=%s",
                    map_id,
                    zone_id,
                    guid,
                    ",".join(active_owner_remote_ids),
                )
                return
            conn.connection_state = "idle"
            conn.last_seen_ms = now_ms
            name = conn.name
            lat = conn.lat
            lng = conn.lng
            caltopo_rtt_ms = conn.caltopo_rtt_ms
            app_version = conn.app_version
            app_version_code = conn.app_version_code
            reported_map_id = conn.reported_map_id
            coordination_mode = conn.coordination_mode
        logger.info(
            "r2c zone_idle: map=%s zone=%s guid=%s",
            map_id,
            zone_id,
            guid,
        )
        await self._upsert_zone_state(
            organization_id,
            map_id,
            zone_id,
            guid or zone_id,
            name,
            lat,
            lng,
            caltopo_rtt_ms,
            False,
            now_ms,
            reported_map_id,
            coordination_mode,
            "idle",
            app_version,
            app_version_code,
        )
        await self.broadcast_zone_update(organization_id, map_id)

    async def _handle_heartbeat(self, websocket: WebSocket, payload: dict):
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        owner_updates: list[tuple[str, str, str, dict]] = []
        owner_deletes: list[tuple[str, str, str]] = []
        confirmation_replays: list[dict] = []
        owner_lease_expire_ms = 0
        old_map_id_for_update: Optional[str] = None
        async with self._lock:
            conn = self._connections.get(websocket)
            if conn is None:
                return
            organization_id = conn.organization_id
            conn.lat = float(payload.get("lat", conn.lat) or 0.0)
            conn.lng = float(payload.get("lng", conn.lng) or 0.0)
            incoming_rtt_ms = self._parse_caltopo_rtt_ms(payload.get("caltopoRttMs"))
            if incoming_rtt_ms > 0:
                conn.caltopo_rtt_ms = incoming_rtt_ms
            conn.last_seen_ms = now_ms
            if conn.coordination_mode == self.COORDINATION_MODE_STANDALONE:
                resolved_map_id, resolved_mode = self._resolve_coordination_map_id(
                    organization_id,
                    conn.reported_map_id,
                    conn.zone_id or "",
                    conn.guid or "",
                    conn.lat,
                    conn.lng,
                )
                if resolved_map_id.startswith(self.STANDALONE_PREFIX):
                    persisted_map_id = await self._resolve_persisted_mapped_coordination_map_id(
                        organization_id,
                        conn.zone_id or "",
                        conn.lat,
                        conn.lng,
                        now_ms,
                    )
                    if persisted_map_id:
                        resolved_map_id = persisted_map_id
                        resolved_mode = self.COORDINATION_MODE_STANDALONE
                if resolved_map_id and resolved_map_id != conn.map_id:
                    old_map_id_for_update = conn.map_id
                    old_zone_id = conn.zone_id
                    if old_map_id_for_update and old_zone_id:
                        old_scope_key = self._scope_key(organization_id, old_map_id_for_update)
                        old_zones = self._zones_by_map.get(old_scope_key, {})
                        if old_zones.get(old_zone_id) is conn:
                            old_zones.pop(old_zone_id, None)
                        if not old_zones:
                            self._zones_by_map.pop(old_scope_key, None)
                    conn.map_id = resolved_map_id
                    conn.coordination_mode = resolved_mode
                    if old_zone_id:
                        self._zones_by_map.setdefault(self._scope_key(organization_id, resolved_map_id), {})[old_zone_id] = conn
                    logger.info(
                        "r2c standalone rehomed: old_map=%s new_map=%s zone=%s guid=%s",
                        old_map_id_for_update,
                        resolved_map_id,
                        old_zone_id,
                        conn.guid or old_zone_id,
                    )
                    confirmation_replays.extend(
                        self._merge_drone_confirmations_locked(organization_id, old_map_id_for_update, resolved_map_id, now_ms)
                    )
                    confirmation_replays.extend(
                        self._recent_drone_confirmations_locked(organization_id, resolved_map_id, now_ms)
                    )
                    confirmation_replays = list({
                        str(event.get("remoteId", "")): event
                        for event in confirmation_replays
                        if event.get("remoteId")
                    }.values())
                    for (owner_organization_id, owner_map_id, remote_id), owner in list(self._owners.items()):
                        if owner_organization_id != organization_id or owner_map_id != old_map_id_for_update or owner.get("owner_guid") != conn.guid:
                            continue
                        self._owners.pop((owner_organization_id, owner_map_id, remote_id), None)
                        existing = self._owners.get((organization_id, resolved_map_id, remote_id))
                        if existing is None:
                            self._owners[(organization_id, resolved_map_id, remote_id)] = owner
                            owner_updates.append((organization_id, resolved_map_id, remote_id, dict(owner)))
                        else:
                            chosen_owner = self._pick_owner(existing, owner)
                            self._owners[(organization_id, resolved_map_id, remote_id)] = chosen_owner
                            owner_updates.append((organization_id, resolved_map_id, remote_id, dict(chosen_owner)))
                        owner_deletes.append((organization_id, owner_map_id, remote_id))
            map_id = conn.map_id
            zone_id = conn.zone_id
            guid = conn.guid
            name = conn.name
            lat = conn.lat
            lng = conn.lng
            caltopo_rtt_ms = conn.caltopo_rtt_ms
            app_version = conn.app_version
            app_version_code = conn.app_version_code
            reported_map_id = conn.reported_map_id
            coordination_mode = conn.coordination_mode
            if guid:
                for (owner_organization_id, owner_map_id, remote_id), owner in self._owners.items():
                    if owner_organization_id == organization_id and owner.get("owner_guid") == guid:
                        owner["lease_expire_ms"] = now_ms + (R2C_LEASE_SEC * 1000)
                        owner_lease_expire_ms = max(owner_lease_expire_ms, int(owner["lease_expire_ms"]))
                        owner_updates.append((organization_id, owner_map_id, remote_id, dict(owner)))
        if map_id and zone_id:
            await self._upsert_zone_state(
                organization_id,
                map_id,
                zone_id,
                guid or zone_id,
                name,
                lat,
                lng,
                caltopo_rtt_ms,
                True,
                now_ms,
                reported_map_id,
                coordination_mode,
                "online",
                app_version,
                app_version_code,
            )
        if old_map_id_for_update and zone_id:
            await self._delete_zone_state(organization_id, old_map_id_for_update, zone_id)
        for owner_organization_id, owner_map_id, remote_id, owner in owner_updates:
            await self._upsert_owner_state(owner_organization_id, owner_map_id, remote_id, owner)
        for owner_organization_id, owner_map_id, remote_id in owner_deletes:
            await self._delete_owner_state(owner_organization_id, owner_map_id, remote_id)
        logger.debug(
            "r2c heartbeat_ack: map=%s zone=%s guid=%s client_seq=%s owner_lease_expire_ts=%s",
            map_id,
            zone_id,
            guid,
            payload.get("seq"),
            owner_lease_expire_ms,
        )
        await websocket.send_text(json.dumps({
            "type": "heartbeat_ack",
            "serverTime": now_ms,
            "mapId": map_id,
            "zoneId": zone_id,
            "guid": guid,
            "leaseSec": R2C_LEASE_SEC,
            "ownerLeaseExpireTs": owner_lease_expire_ms,
            "clientSeq": payload.get("seq"),
        }))
        if map_id:
            await self.broadcast_zone_update(organization_id, map_id, force=False)
        if old_map_id_for_update and old_map_id_for_update != map_id:
            await self.broadcast_zone_update(organization_id, old_map_id_for_update)
        for event in confirmation_replays:
            if map_id:
                await self._broadcast_drone_confirmation(organization_id, map_id, event)

    async def _handle_first_sighting(self, websocket: WebSocket, payload: dict):
        remote_id = payload.get("remoteId", "")
        should_persist_owner = True
        async with self._lock:
            organization_id, map_id, zone_id, guid = self._message_context(websocket, payload)
            if not map_id or not remote_id or not zone_id:
                return
            now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
            existing = self._owners.get((organization_id, map_id, remote_id))
            candidate = {
                "owner_guid": guid,
                "owner_zone_id": zone_id,
                "drone_ts": int(payload.get("droneTs", 0) or 0),
                "distance_m": float(payload.get("distanceFromZoneM", 0.0) or 0.0),
                "mapped_id": payload.get("mappedId", "") or "",
                "lease_seq": 1
            }
            decision_reason = "initial_claim"
            if existing is None:
                owner = candidate
            elif existing.get("source") == "confirmation":
                if int(existing.get("lease_expire_ms", 0) or 0) >= now_ms:
                    owner = existing
                    decision_reason = "confirmed_owner_active"
                    should_persist_owner = False
                else:
                    owner = candidate
                    candidate["lease_seq"] = int(existing.get("lease_seq", 0)) + 1
                    decision_reason = "confirmed_owner_expired"
            else:
                owner = self._pick_owner(existing, candidate)
                decision_reason = "candidate_better" if owner is candidate else "existing_better"
                if owner is candidate:
                    candidate["lease_seq"] = int(existing.get("lease_seq", 0)) + 1
            if should_persist_owner:
                owner["lease_expire_ms"] = now_ms + (R2C_LEASE_SEC * 1000)
                self._owners[(organization_id, map_id, remote_id)] = owner
        logger.info(
            "r2c owner_decision: map=%s remote_id=%s reason=%s prev_owner_guid=%s prev_zone_id=%s "
            "prev_drone_ts=%s prev_distance_m=%s prev_mapped_id=%s candidate_guid=%s candidate_zone_id=%s "
            "candidate_drone_ts=%s candidate_distance_m=%s candidate_mapped_id=%s chosen_owner_guid=%s "
            "chosen_zone_id=%s lease_seq=%s lease_expire_ts=%s",
            map_id,
            remote_id,
            decision_reason,
            (existing or {}).get("owner_guid", ""),
            (existing or {}).get("owner_zone_id", ""),
            (existing or {}).get("drone_ts", 0),
            (existing or {}).get("distance_m", 0.0),
            (existing or {}).get("mapped_id", ""),
            candidate.get("owner_guid", ""),
            candidate.get("owner_zone_id", ""),
            candidate.get("drone_ts", 0),
            candidate.get("distance_m", 0.0),
            candidate.get("mapped_id", ""),
            owner.get("owner_guid", ""),
            owner.get("owner_zone_id", ""),
            owner.get("lease_seq", 0),
            owner.get("lease_expire_ms", 0),
        )
        if should_persist_owner:
            await self._upsert_owner_state(organization_id, map_id, remote_id, owner)
            await self.broadcast(
                organization_id,
                map_id,
                {
                    "type": "owner_assigned",
                    "remoteId": remote_id,
                    "ownerGuid": owner["owner_guid"],
                    "ownerZoneId": owner["owner_zone_id"],
                    "leaseSeq": owner["lease_seq"],
                    "leaseExpireTs": owner["lease_expire_ms"]
                }
            )

    async def _handle_sighting(self, websocket: WebSocket, payload: dict):
        remote_id = payload.get("remoteId", "")
        owner_refresh: Optional[tuple[str, str, str, dict]] = None
        async with self._lock:
            organization_id, map_id, from_zone_id, guid = self._message_context(websocket, payload)
            if not map_id or not remote_id:
                return
            owner = self._owners.get((organization_id, map_id, remote_id))
            if owner is None:
                return
            owner_zone_id = owner.get("owner_zone_id", "")
            if from_zone_id and owner_zone_id == from_zone_id:
                now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
                owner["lease_expire_ms"] = now_ms + (R2C_LEASE_SEC * 1000)
                owner_refresh = (organization_id, map_id, remote_id, dict(owner))
                logger.info(
                    "r2c owner_sighting_refresh: map=%s remote_id=%s owner_zone_id=%s lease_expire_ts=%s",
                    map_id,
                    remote_id,
                    owner_zone_id,
                    owner["lease_expire_ms"],
                )
                target = None
            else:
                zones = self._zones_by_map.get(self._scope_key(organization_id, map_id), {})
                target = zones.get(owner_zone_id)
        if owner_refresh is not None:
            owner_organization_id, owner_map_id, owner_remote_id, owner_state = owner_refresh
            await self._upsert_owner_state(owner_organization_id, owner_map_id, owner_remote_id, owner_state)
            return
        if target is None or target.websocket is None:
            logger.warning(
                "relay_sighting skipped: owner unavailable map=%s remote_id=%s owner_zone_id=%s",
                map_id,
                remote_id,
                owner_zone_id,
            )
            return
        relay = dict(payload)
        relay["type"] = "relay_sighting"
        relay["mapId"] = map_id
        relay["fromZoneId"] = from_zone_id
        await self._record_sighting(
            organization_id,
            map_id,
            remote_id,
            relay.get("fromZoneId", ""),
            guid,
            int(payload.get("droneTs", 0) or 0),
            float(payload.get("lat", 0.0) or 0.0),
            float(payload.get("lng", 0.0) or 0.0),
            float(payload.get("altM", 0.0) or 0.0)
        )
        try:
            await target.websocket.send_text(json.dumps(relay))
        except Exception as e:
            logger.warning("relay_sighting failed for %s/%s: %s", map_id, remote_id, e)

    async def _handle_drone_confirmed(self, websocket: WebSocket, payload: dict):
        remote_id = payload.get("remoteId", "")
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        async with self._lock:
            conn = self._connections.get(websocket)
            if conn is None:
                return
            organization_id, map_id, zone_id, guid = self._message_context(websocket, payload)
            if not map_id or not remote_id:
                return
            self._confirmation_event_seq += 1
            event = {
                "type": "drone_confirmed",
                "mapId": map_id,
                "remoteId": remote_id,
                "zoneId": zone_id,
                "guid": guid,
                "confirmedByGuid": guid,
                "mappedId": payload.get("mappedId", "") or "",
                "trackLabel": payload.get("trackLabel", "") or payload.get("mappedId", "") or "",
                "org": payload.get("org", "") or "",
                "model": payload.get("model", "") or "",
                "ownerName": payload.get("ownerName", "") or "",
                "confirmedAtMs": now_ms,
                "confirmationEventId": self._confirmation_event_seq,
            }
            existing_owner = self._owners.get((organization_id, map_id, remote_id))
            owner = {
                "owner_guid": event.get("confirmedByGuid", "") or event.get("guid", ""),
                "owner_zone_id": zone_id,
                "drone_ts": int((existing_owner or {}).get("drone_ts", 0) or 0),
                "distance_m": float((existing_owner or {}).get("distance_m", 0.0) or 0.0),
                "mapped_id": event["mappedId"],
                "lease_seq": int((existing_owner or {}).get("lease_seq", 0) or 0) + 1,
                "lease_expire_ms": now_ms + (R2C_LEASE_SEC * 1000),
                "source": "confirmation",
            }
            self._remember_drone_confirmation_locked(organization_id, map_id, event, now_ms)
            self._owners[(organization_id, map_id, remote_id)] = owner
        await self._upsert_confirmation_state(organization_id, map_id, event, now_ms)
        await self._upsert_owner_state(organization_id, map_id, remote_id, owner)
        logger.info(
            "r2c drone_confirmed: map=%s remote_id=%s confirmed_by=%s mapped_id=%s",
            map_id,
            remote_id,
            event["confirmedByGuid"],
            event["mappedId"],
        )
        await self._broadcast_drone_confirmation(organization_id, map_id, event)
        await self.broadcast(
            organization_id,
            map_id,
            {
                "type": "owner_assigned",
                "remoteId": remote_id,
                "ownerGuid": owner["owner_guid"],
                "ownerZoneId": owner["owner_zone_id"],
                "leaseSeq": owner["lease_seq"],
                "leaseExpireTs": owner["lease_expire_ms"]
            }
        )

    async def _handle_drone_lost(self, websocket: WebSocket, payload: dict):
        remote_id = payload.get("remoteId", "")
        expired = False
        async with self._lock:
            organization_id, map_id, zone_id, guid = self._message_context(websocket, payload)
            if not map_id or not remote_id:
                return
            owner = self._owners.get((organization_id, map_id, remote_id))
            if owner and owner.get("owner_zone_id") == zone_id:
                self._owners.pop((organization_id, map_id, remote_id), None)
                self._forget_drone_confirmation_locked(organization_id, map_id, remote_id)
                expired = True
        if expired:
            logger.info(
                "r2c owner_expired: map=%s remote_id=%s reason=drone_lost prev_owner_guid=%s prev_zone_id=%s",
                map_id,
                remote_id,
                guid,
                zone_id,
            )
            await self._delete_owner_state(organization_id, map_id, remote_id)
            await self._delete_confirmation_state(organization_id, map_id, remote_id)
            await self.broadcast(
                organization_id,
                map_id,
                {
                    "type": "owner_expired",
                    "remoteId": remote_id,
                    "prevOwnerGuid": guid
                }
            )

    async def broadcast_zone_update(self, organization_id: str, map_id: str, force: bool = True):
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        scope_key = self._scope_key(organization_id, map_id)
        async with self._lock:
            if not force:
                if R2C_HEARTBEAT_ZONE_UPDATE_SEC <= 0:
                    return
                last_update_ms = self._last_heartbeat_zone_update_ms_by_map.get(scope_key, 0)
                min_gap_ms = R2C_HEARTBEAT_ZONE_UPDATE_SEC * 1000
                if last_update_ms > 0 and now_ms - last_update_ms < min_gap_ms:
                    return
                self._last_heartbeat_zone_update_ms_by_map[scope_key] = now_ms
            zones = list(self._zones_by_map.get(scope_key, {}).values())
        await self.broadcast(
            organization_id,
            map_id,
            {
                "type": "zone_update",
                "zones": [
                    {
                        "zoneId": zone.zone_id,
                        "guid": zone.guid,
                        "name": zone.name,
                        "appVersion": zone.app_version,
                        "appVersionCode": zone.app_version_code,
                        "lat": zone.lat,
                        "lng": zone.lng,
                        "caltopoRttMs": zone.caltopo_rtt_ms,
                        "lastSeenMs": zone.last_seen_ms,
                        "online": zone.websocket is not None and zone.connection_state == "online",
                        "connectionState": zone.connection_state
                    }
                    for zone in zones if zone.zone_id
                ]
            }
        )

    async def broadcast(self, organization_id: str, map_id: str, payload: dict):
        text = json.dumps(payload)
        async with self._lock:
            recipients = [zone for zone in self._zones_by_map.get(self._scope_key(organization_id, map_id), {}).values() if zone.websocket is not None]
        for zone in recipients:
            try:
                await zone.websocket.send_text(text)
            except Exception as e:
                logger.warning("broadcast failed for %s/%s: %s", map_id, zone.zone_id, e)

    async def _load_state(self):
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        async with AsyncSessionLocal() as session:
            zone_result = await session.execute(select(R2CZoneState).where(R2CZoneState.last_seen_ms >= now_ms - (R2C_LEASE_SEC * 1000)))
            owner_result = await session.execute(select(R2CDroneOwnerState).where(R2CDroneOwnerState.lease_expire_ms >= now_ms))
            zones = zone_result.scalars().all()
            owners = owner_result.scalars().all()
            for zone in zones:
                zone.online = False
                zone.connection_state = "disconnected"
            await session.commit()
        async with self._lock:
            self._zones_by_map.clear()
            for zone in zones:
                organization_id = (
                    getattr(zone, "organization_id", "")
                    or LEGACY_COORDINATION_ORGANIZATION_ID
                )
                conn = R2CZoneConnection(None, organization_id=organization_id)
                conn.map_id = zone.map_id
                conn.reported_map_id = getattr(zone, "reported_map_id", "") or ""
                conn.coordination_mode = getattr(zone, "coordination_mode", "") or self.COORDINATION_MODE_MAP
                conn.zone_id = zone.zone_id
                conn.guid = zone.guid
                conn.name = zone.name
                conn.app_version = getattr(zone, "app_version", "") or ""
                conn.app_version_code = int(getattr(zone, "app_version_code", 0) or 0)
                conn.lat = zone.lat
                conn.lng = zone.lng
                conn.caltopo_rtt_ms = zone.caltopo_rtt_ms
                conn.connection_state = getattr(zone, "connection_state", "") or "disconnected"
                conn.last_seen_ms = zone.last_seen_ms
                self._zones_by_map.setdefault(self._scope_key(organization_id, zone.map_id), {})[zone.zone_id] = conn
            self._owners = {
                (
                    getattr(owner, "organization_id", "")
                    or LEGACY_COORDINATION_ORGANIZATION_ID,
                    owner.map_id,
                    owner.remote_id,
                ): {
                    "owner_guid": owner.owner_guid,
                    "owner_zone_id": owner.owner_zone_id,
                    "drone_ts": owner.first_drone_ts,
                    "distance_m": owner.first_distance_m,
                    "mapped_id": owner.mapped_id,
                    "lease_seq": owner.lease_seq,
                    "lease_expire_ms": owner.lease_expire_ms
                }
                for owner in owners
            }

    async def _load_state_safe(self):
        try:
            await asyncio.wait_for(self._load_state(), timeout=8)
        except asyncio.TimeoutError:
            logger.warning("R2C coordination state load timed out; starting with empty in-memory state")
        except Exception as e:
            logger.warning("R2C coordination state load failed: %s", e)

    async def _cleanup_loop(self):
        try:
            await self._cleanup_persisted_state_safe()
            while True:
                await asyncio.sleep(R2C_DB_CLEANUP_SEC)
                await self._cleanup_persisted_state_safe()
        except asyncio.CancelledError:
            raise

    async def _cleanup_persisted_state_safe(self):
        try:
            await asyncio.wait_for(self._cleanup_persisted_state(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("R2C persisted-state cleanup timed out")
        except Exception as e:
            logger.warning("R2C persisted-state cleanup failed: %s", e)

    async def _cleanup_persisted_state(self):
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        stale_zone_cutoff_ms = now_ms - (R2C_LEASE_SEC * 1000)
        stale_sighting_cutoff_ms = now_ms - (R2C_LEASE_SEC * 1000 * 4)
        deleted_zone_count = 0
        deleted_owner_count = 0
        deleted_sighting_count = 0
        async with AsyncSessionLocal() as session:
            stale_zones = await session.execute(
                select(R2CZoneState).where(R2CZoneState.last_seen_ms < stale_zone_cutoff_ms)
            )
            for state in stale_zones.scalars().all():
                await session.delete(state)
                deleted_zone_count += 1

            stale_owners = await session.execute(
                select(R2CDroneOwnerState).where(R2CDroneOwnerState.lease_expire_ms < now_ms)
            )
            for state in stale_owners.scalars().all():
                await session.delete(state)
                deleted_owner_count += 1

            stale_sightings = await session.execute(
                select(R2CRecentSighting).where(R2CRecentSighting.received_ms < stale_sighting_cutoff_ms)
            )
            for state in stale_sightings.scalars().all():
                await session.delete(state)
                deleted_sighting_count += 1

            await session.commit()
        deleted_preflight_count = 0
        if control_plane_store is not None:
            deleted_preflight_count = (
                await control_plane_store
                .cleanup_expired_video_preflight_exchanges()
            )
        if (
            deleted_zone_count
            or deleted_owner_count
            or deleted_sighting_count
            or deleted_preflight_count
        ):
            logger.info(
                "R2C persisted-state cleanup removed zones=%s owners=%s "
                "sightings=%s video_preflights=%s",
                deleted_zone_count,
                deleted_owner_count,
                deleted_sighting_count,
                deleted_preflight_count,
            )

    async def _expiry_loop(self):
        try:
            while True:
                await asyncio.sleep(R2C_SWEEP_SEC)
                await self.expire_stale_entries()
        except asyncio.CancelledError:
            raise

    async def expire_stale_entries(self):
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        stale_cutoff_ms = now_ms - (R2C_LEASE_SEC * 1000)
        expired_maps: set[tuple[str, str]] = set()
        expired_owners: list[tuple[str, str, str, str]] = []
        async with self._lock:
            for scope_key, zones in list(self._zones_by_map.items()):
                for zone_id, zone in list(zones.items()):
                    if zone.websocket is not None:
                        continue
                    if zone.last_seen_ms < stale_cutoff_ms:
                        zones.pop(zone_id, None)
                        expired_maps.add(scope_key)
                if not zones:
                    self._zones_by_map.pop(scope_key, None)
            for (organization_id, map_id, remote_id), owner in list(self._owners.items()):
                if int(owner.get("lease_expire_ms", 0) or 0) < now_ms:
                    expired_owners.append((organization_id, map_id, remote_id, owner.get("owner_guid", "")))
                    self._owners.pop((organization_id, map_id, remote_id), None)
                    self._forget_drone_confirmation_locked(organization_id, map_id, remote_id)
        for organization_id, map_id in expired_maps:
            await self._delete_stale_zones(organization_id, map_id, stale_cutoff_ms)
            await self.broadcast_zone_update(organization_id, map_id)
        for organization_id, map_id, remote_id, owner_guid in expired_owners:
            logger.info(
                "r2c owner_expired: map=%s remote_id=%s reason=lease_timeout prev_owner_guid=%s",
                map_id,
                remote_id,
                owner_guid,
            )
            await self._delete_owner_state(organization_id, map_id, remote_id)
            await self._delete_confirmation_state(organization_id, map_id, remote_id)
            await self.broadcast(
                organization_id,
                map_id,
                {
                    "type": "owner_expired",
                    "remoteId": remote_id,
                    "prevOwnerGuid": owner_guid
                }
            )

    async def _upsert_zone_state(self, organization_id: str, map_id: str, zone_id: str, guid: str, name: str,
                                 lat: float, lng: float, caltopo_rtt_ms: int,
                                 online: bool, last_seen_ms: int,
                                 reported_map_id: str = "",
                                 coordination_mode: str = "map",
                                 connection_state: str = "",
                                 app_version: str = "",
                                 app_version_code: int = 0):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CZoneState).where(
                    R2CZoneState.organization_id == organization_id,
                    R2CZoneState.map_id == map_id,
                    R2CZoneState.zone_id == zone_id
                )
            )
            state = result.scalar_one_or_none()
            if state is None:
                state = R2CZoneState(organization_id=organization_id, map_id=map_id, zone_id=zone_id)
                session.add(state)
            state.reported_map_id = reported_map_id
            state.coordination_mode = coordination_mode
            state.guid = guid
            state.name = name
            state.app_version = app_version
            state.app_version_code = app_version_code
            state.lat = lat
            state.lng = lng
            state.caltopo_rtt_ms = caltopo_rtt_ms
            state.online = online
            state.connection_state = connection_state or ("online" if online else "disconnected")
            state.last_seen_ms = last_seen_ms
            await session.commit()

    async def _delete_zone_state(self, organization_id: str, map_id: str, zone_id: str):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CZoneState).where(
                    R2CZoneState.organization_id == organization_id,
                    R2CZoneState.map_id == map_id,
                    R2CZoneState.zone_id == zone_id
                )
            )
            state = result.scalar_one_or_none()
            if state is not None:
                await session.delete(state)
                await session.commit()

    async def _delete_stale_zones(self, organization_id: str, map_id: str, cutoff_ms: int):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CZoneState).where(
                    R2CZoneState.organization_id == organization_id,
                    R2CZoneState.map_id == map_id,
                    R2CZoneState.last_seen_ms < cutoff_ms
                )
            )
            for state in result.scalars().all():
                await session.delete(state)
            await session.commit()

    async def _upsert_owner_state(self, organization_id: str, map_id: str, remote_id: str, owner: dict):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CDroneOwnerState).where(
                    R2CDroneOwnerState.organization_id == organization_id,
                    R2CDroneOwnerState.map_id == map_id,
                    R2CDroneOwnerState.remote_id == remote_id
                )
            )
            state = result.scalar_one_or_none()
            if state is None:
                state = R2CDroneOwnerState(organization_id=organization_id, map_id=map_id, remote_id=remote_id)
                session.add(state)
            state.owner_guid = owner.get("owner_guid", "")
            state.owner_zone_id = owner.get("owner_zone_id", "")
            state.first_drone_ts = int(owner.get("drone_ts", 0) or 0)
            state.first_distance_m = float(owner.get("distance_m", 0.0) or 0.0)
            state.mapped_id = owner.get("mapped_id", "") or ""
            state.lease_seq = int(owner.get("lease_seq", 0) or 0)
            state.lease_expire_ms = int(owner.get("lease_expire_ms", 0) or 0)
            state.updated_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
            await session.commit()

    async def _delete_owner_state(self, organization_id: str, map_id: str, remote_id: str):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CDroneOwnerState).where(
                    R2CDroneOwnerState.organization_id == organization_id,
                    R2CDroneOwnerState.map_id == map_id,
                    R2CDroneOwnerState.remote_id == remote_id
                )
            )
            state = result.scalar_one_or_none()
            if state is not None:
                await session.delete(state)
                await session.commit()

    async def _upsert_confirmation_state(self, organization_id: str, map_id: str, event: dict, confirmed_at_ms: int):
        remote_id = str(event.get("remoteId", "") or "")
        if not map_id or not remote_id:
            return
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CDroneConfirmationState).where(
                    R2CDroneConfirmationState.organization_id == organization_id,
                    R2CDroneConfirmationState.map_id == map_id,
                    R2CDroneConfirmationState.remote_id == remote_id
                )
            )
            state = result.scalar_one_or_none()
            if state is None:
                state = R2CDroneConfirmationState(organization_id=organization_id, map_id=map_id, remote_id=remote_id)
                session.add(state)
            state.zone_id = event.get("zoneId", "") or ""
            state.guid = event.get("guid", "") or ""
            state.confirmed_by_guid = event.get("confirmedByGuid", "") or ""
            state.mapped_id = event.get("mappedId", "") or ""
            state.track_label = event.get("trackLabel", "") or ""
            state.org = event.get("org", "") or ""
            state.model = event.get("model", "") or ""
            state.owner_name = event.get("ownerName", "") or ""
            state.confirmed_at_ms = confirmed_at_ms
            cutoff_ms = confirmed_at_ms - self.CONFIRMATION_RETENTION_MS
            result = await session.execute(
                select(R2CDroneConfirmationState).where(
                    R2CDroneConfirmationState.confirmed_at_ms < cutoff_ms
                )
            )
            for stale in result.scalars().all():
                await session.delete(stale)
            await session.commit()

    async def _delete_confirmation_state(self, organization_id: str, map_id: str, remote_id: str):
        if not map_id or not remote_id:
            return
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CDroneConfirmationState).where(
                    R2CDroneConfirmationState.organization_id == organization_id,
                    R2CDroneConfirmationState.map_id == map_id,
                    R2CDroneConfirmationState.remote_id == remote_id
                )
            )
            deleted_count = 0
            for state in result.scalars().all():
                await session.delete(state)
                deleted_count += 1
            await session.commit()
        if deleted_count:
            logger.info(
                "r2c cleared drone confirmation state for ended flight: map=%s remote_id=%s count=%s",
                map_id,
                remote_id,
                deleted_count,
            )

    async def _delete_confirmation_state_for_zone(self, organization_id: str, map_id: str, guid: str, zone_id: str):
        if not map_id or (not guid and not zone_id):
            return
        async with AsyncSessionLocal() as session:
            conditions = [
                R2CDroneConfirmationState.organization_id == organization_id,
                R2CDroneConfirmationState.map_id == map_id,
            ]
            zone_conditions = []
            if guid:
                zone_conditions.append(R2CDroneConfirmationState.confirmed_by_guid == guid)
                zone_conditions.append(R2CDroneConfirmationState.guid == guid)
            if zone_id:
                zone_conditions.append(R2CDroneConfirmationState.zone_id == zone_id)
            result = await session.execute(
                select(R2CDroneConfirmationState).where(and_(*conditions), or_(*zone_conditions))
            )
            deleted_count = 0
            for state in result.scalars().all():
                await session.delete(state)
                deleted_count += 1
            await session.commit()
        if deleted_count:
            logger.info(
                "r2c cleared drone confirmation state for disconnected zone: map=%s zone=%s guid=%s count=%s",
                map_id,
                zone_id,
                guid,
                deleted_count,
            )

    async def _load_recent_confirmation_events(self, organization_id: str, map_id: str, now_ms: int) -> list[dict]:
        cutoff_ms = now_ms - self.CONFIRMATION_RETENTION_MS
        recent_zone_cutoff_ms = now_ms - (R2C_LEASE_SEC * 1000)
        async with AsyncSessionLocal() as session:
            confirmation_result = await session.execute(
                select(R2CDroneConfirmationState).where(
                    R2CDroneConfirmationState.organization_id == organization_id,
                    R2CDroneConfirmationState.map_id == map_id,
                    R2CDroneConfirmationState.confirmed_at_ms >= cutoff_ms
                )
            )
            zone_result = await session.execute(
                select(R2CZoneState).where(
                    R2CZoneState.organization_id == organization_id,
                    R2CZoneState.map_id == map_id,
                    R2CZoneState.online == True,
                    R2CZoneState.last_seen_ms >= recent_zone_cutoff_ms,
                )
            )
            active_zone_keys = set()
            for zone in zone_result.scalars().all():
                active_zone_keys.add(str(zone.zone_id or ""))
                active_zone_keys.add(str(zone.guid or ""))
            events = []
            stale_states = []
            for state in confirmation_result.scalars().all():
                confirmer_keys = {
                    str(state.confirmed_by_guid or ""),
                    str(state.guid or ""),
                    str(state.zone_id or ""),
                }
                if not any(key and key in active_zone_keys for key in confirmer_keys):
                    stale_states.append(state)
                    continue
                events.append({
                    "type": "drone_confirmed",
                    "mapId": state.map_id,
                    "remoteId": state.remote_id,
                    "zoneId": state.zone_id,
                    "guid": state.guid,
                    "confirmedByGuid": state.confirmed_by_guid,
                    "mappedId": state.mapped_id or "",
                    "trackLabel": state.track_label or state.mapped_id or "",
                    "org": state.org or "",
                    "model": state.model or "",
                    "ownerName": state.owner_name or "",
                    "confirmedAtMs": int(state.confirmed_at_ms or 0),
                })
            for state in stale_states:
                await session.delete(state)
            if stale_states:
                await session.commit()
            return events

    async def _record_sighting(self, organization_id: str, map_id: str, remote_id: str, zone_id: str, guid: str,
                               drone_ts: int, lat: float, lng: float, alt_m: float):
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        async with AsyncSessionLocal() as session:
            session.add(R2CRecentSighting(
                organization_id=organization_id,
                map_id=map_id,
                remote_id=remote_id,
                zone_id=zone_id,
                guid=guid,
                drone_ts=drone_ts,
                lat=lat,
                lng=lng,
                alt_m=alt_m,
                received_ms=now_ms
            ))
            cutoff_ms = now_ms - (R2C_LEASE_SEC * 1000 * 4)
            result = await session.execute(select(R2CRecentSighting).where(R2CRecentSighting.received_ms < cutoff_ms))
            for sighting in result.scalars().all():
                await session.delete(sighting)
            await session.commit()

    @staticmethod
    def _pick_owner(existing: dict, candidate: dict) -> dict:
        existing_key = (
            int(existing.get("drone_ts", 0) or 0),
            float(existing.get("distance_m", 0.0) or 0.0),
            0 if existing.get("mapped_id") else 1,
            str(existing.get("owner_guid", ""))
        )
        candidate_key = (
            int(candidate.get("drone_ts", 0) or 0),
            float(candidate.get("distance_m", 0.0) or 0.0),
            0 if candidate.get("mapped_id") else 1,
            str(candidate.get("owner_guid", ""))
        )
        return candidate if candidate_key < existing_key else existing

    @staticmethod
    def _parse_caltopo_rtt_ms(value) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0

    @staticmethod
    def _parse_nonnegative_int(value) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed >= 0 else 0

    async def get_connection_debug_info(self, websocket: WebSocket) -> dict:
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        async with self._lock:
            conn = self._connections.get(websocket)
            if conn is None:
                return {}
            return {
                "map_id": conn.map_id or "",
                "zone_id": conn.zone_id or "",
                "guid": conn.guid or "",
                "conn_age_ms": max(now_ms - int(conn.connected_at_ms or now_ms), 0),
                "hello_age_ms": max(now_ms - int(conn.hello_received_at_ms or conn.connected_at_ms or now_ms), 0),
                "last_seen_age_ms": max(now_ms - int(conn.last_seen_ms or 0), 0),
            }

    async def connection_count(self) -> int:
        async with self._lock:
            return len(self._connections)


r2c_hub = R2CCoordinationHub()



# --- CORS MIDDLEWARE ---
# This allows your drone app to send PUT requests without being blocked
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(CORS_ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", API_KEY_NAME],
)

templates = Jinja2Templates(directory="templates")

# --- HELPER FUNCTIONS ---
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
        
security = HTTPBasic()


def clear_platform_admin_session(request: Request) -> None:
    """Remove platform-admin authentication without signing out an org user."""
    prefixes = ("platform_admin_", "_platform_", "_csrf_platform_")
    for key in tuple(request.session):
        if key.startswith(prefixes):
            request.session.pop(key, None)


def clear_organization_session(request: Request) -> None:
    """Remove organization authentication without signing out the platform admin."""
    prefixes = ("organization_", "_organization_", "_csrf_organization_")
    for key in tuple(request.session):
        if key.startswith(prefixes):
            request.session.pop(key, None)


async def organization_session_memberships(
        request: Request, current_user):
    """Resolve memberships from the externally verified session identity."""
    external_identity = request.session.get("organization_external_identity")
    if isinstance(external_identity, dict):
        provider = str(external_identity.get("provider", ""))
        issuer = str(external_identity.get("issuer", ""))
        subject = str(external_identity.get("subject", ""))
        if provider and issuer and subject:
            return await control_plane_store.list_active_users_by_external_identity(
                provider=provider,
                issuer=issuer,
                subject=subject,
            )
    if request.session.get("organization_google_subject"):
        return await control_plane_store.list_active_users_by_email(current_user.email)
    return (current_user,)


def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not LEGACY_ADMIN_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="The global administration interface has been retired.",
        )
    if not TRACKER_ADMIN_PASS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Legacy administration is not configured.",
        )
    if credentials is None:
        return False
    if (not secrets.compare_digest(credentials.username, TRACKER_ADMIN_USER)
        or not secrets.compare_digest(credentials.password, TRACKER_ADMIN_PASS)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"}
        )
    return credentials.username


async def check_platform_admin(request: Request):
    if control_plane_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform administration is not configured.",
        )
    identity, authoritative_user = await current_platform_admin_identity()
    user_id = request.session.get("platform_admin_user_id")
    session_generation = request.session.get("platform_admin_identity_generation")
    user = (
        await control_plane_store.get_platform_admin(user_id)
        if isinstance(user_id, str)
        else None
    )
    if (
        user is None
        or user.id != authoritative_user.id
        or user.email != identity.email
        or session_generation != identity.generation
    ):
        clear_platform_admin_session(request)
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={
                "Location": (
                    "/platform-admin/login?"
                    + urlencode({"next": request.url.path})
                )
            },
        )
    return user


async def current_platform_admin_identity():
    if control_plane_store is None or platform_admin_identity_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform administration is not configured.",
        )
    try:
        identity = await platform_admin_identity_provider.get_current()
        user = await control_plane_store.reconcile_platform_admin_identity(
            email=identity.email,
            display_name=identity.display_name,
        )
        return identity, user
    except (PlatformAdminIdentityError, ControlPlaneError, ValueError):
        logging.exception("Platform administrator identity is unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform administrator identity is temporarily unavailable.",
        )


async def require_organization_user(
        request: Request,
        designator: str,
        required_roles: tuple[str, ...] = (),
        redirect_to_login: bool = False,
        login_next: str = ""):
    if not organization_site_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Organization administration is not configured.",
        )
    organization = await control_plane_store.get_organization(designator)
    user_id = request.session.get("organization_user_id")
    session_designator = request.session.get("organization_designator")
    if (
        organization is None
        or not user_id
        or session_designator != organization.designator
    ):
        if redirect_to_login and organization is not None:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={
                    "Location": f"/{organization.designator.lower()}/login"
                    + ("?" + urlencode({"next": login_next}) if login_next else "")
                },
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organization login required.",
        )
    user = await control_plane_store.get_user(user_id)
    if (
        user is None
        or user.state != "active"
        or user.organization_id != organization.id
    ):
        request.session.pop("organization_user_id", None)
        request.session.pop("organization_designator", None)
        if redirect_to_login:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={
                    "Location": f"/{organization.designator.lower()}/login"
                    + ("?" + urlencode({"next": login_next}) if login_next else "")
                },
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organization login required.",
        )
    if required_roles and not set(required_roles).intersection(user.roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your organization role does not permit this action.",
        )
    return organization, user


async def organization_page_identity(
        request: Request,
        designator: str) -> str:
    """Return the current member name for an organization page, or Guest."""
    if control_plane_store is None:
        return "Guest"
    try:
        organization = await control_plane_store.get_organization(designator)
    except InvalidOrganizationError:
        return "Guest"
    if organization is None:
        return "Guest"
    user_id = request.session.get("organization_user_id")
    session_designator = request.session.get("organization_designator")
    if not user_id or session_designator != organization.designator:
        return "Guest"
    user = await control_plane_store.get_user(user_id)
    if (
        user is None
        or user.state != "active"
        or user.organization_id != organization.id
    ):
        request.session.pop("organization_user_id", None)
        request.session.pop("organization_designator", None)
        return "Guest"
    return user.display_name


def opt_check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials is None or not LEGACY_ADMIN_ENABLED or not TRACKER_ADMIN_PASS:
        return False
    admin_user = secrets.compare_digest(credentials.username, TRACKER_ADMIN_USER)
    admin_pass = secrets.compare_digest(credentials.password, TRACKER_ADMIN_PASS)
    is_admin = (admin_user and admin_pass)
    return is_admin

def get_time_of_day(start_ts_sec, lat, lng):
    utc_time = datetime.fromtimestamp(start_ts_sec, tz=timezone.utc)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        # suppress warnings from suncalc:
        sun_times = get_times(utc_time, lng, lat)
    # Sort keys by time to determine the active range
    # We filter for common phases returned by suncalc-py
    phases = [
        (sun_times['night_end'].timestamp(), "Pre-dawn"),
        (sun_times['nautical_dawn'].timestamp(), "Nautical Dawn"),
        (sun_times['dawn'].timestamp(), "Civil Dawn"),
        (sun_times['sunrise'].timestamp(), "Sunrise"),
        (sun_times['sunrise_end'].timestamp(), "Early Morning"),
        (sun_times['golden_hour_end'].timestamp(), "Morning"),
        (sun_times['solar_noon'].timestamp(), "Afternoon"),
        (sun_times['golden_hour'].timestamp(), "Golden Hour"),
        (sun_times['sunset_start'].timestamp(), "Sunset"),
        (sun_times['sunset'].timestamp(), "Civil Dusk"),
        (sun_times['dusk'].timestamp(), "Nautical Dusk"),
        (sun_times['nautical_dusk'].timestamp(), "Nautical Dusk"),
        (sun_times['night'].timestamp(), "Night")
    ]

    
    phases.sort(key=lambda x: x[0]) ;# Sort by the datetime value
    timeofday_str = "Night"  # Default for times before the first phase (early AM)
    for phase_time, description in phases:
        if start_ts_sec >= phase_time:
            timeofday_str = description
        else:
            break
    return timeofday_str

def parse_prop(prop):
    """Parse a LineString 'properties' dictionary.
    Args:
        prop (dictionary):      Expects a legacy form Caltopo geo-json 
                                properties dict. If generated by RID2Caltopo
                                1.0.5 or later, will include additional drone-
                                specific parameters in a 'r2c_prop' child dict.
    Returns:
        Dictionary containing:
        'incident'  (string):   Optional incident identifier or "Training".
        'op_period' (string):   Optionally a numbered operational period.
        'sar_id' (string):      Callsign of the form '1SAR7'
        'uas' (string):         Shorthand UAS description - usually follows the
                                sar_id.
        'mid' (string):         Mapped ID - this is the normal prefix used in 
                                the track label.
        'rid' (string):         Remote ID - Ground Truth unique identifier per
                                drone.
        'map_id' (string):      ID of the caltopo map - if specified.
        'distance_mi' (float):  precalculated distance - if available.
    """
    pattern = r"(1?[sS][aA][rR][0-9]+)([^_]*)_?.*"
    # r2c_prop not available for legacy track logs:
    incident=""; op_period=""; sar_id=""; uas=""; mid=""; rid=""; map_id=""; distance_mi=0.0
    r2c = prop.get('r2c_prop')
    if r2c:
        incident = r2c.get('incident', "")
        op_period = r2c.get('op_period', "")
        map_id = r2c.get('map_id', "")
        mid = r2c.get('mid', "")
        rid = r2c.get('rid', "")
        uas = r2c.get('model', "")
        distance_mi = float(r2c.get('distance_mi', 0.0))
        match = re.match(pattern, mid)
        if match:
            sar_id = match.group(1)
            uas = match.group(2)
    else:  # title should be available on legacy tracks: 
        title = prop.get('title')
        if not sar_id and title:
            match = re.match(pattern, title)
            if match:
                sar_id = match.group(1)
                uas = match.group(2)
        if not sar_id:
            sar_id = "unknown"
        if not uas:
            match = re.match("([^_]+)_.*", title)
            if match:
                uas = match.group(1)
            else:
                uas = rid
    return {'incident':incident, 'op_period':op_period, 'sar_id':sar_id,
            'uas':uas, 'mid':mid, 'rid':rid, 'map_id':map_id,
            'distance_mi':distance_mi}

def get_weather(ts_sec, lat, lon):
    """Get weather forecast or actual for the specified UTC timestamp at lat, lon
    Args:
        ts_sec (int):      UTC timestamp for given Coordinate.
        lat (float):       Coordinate lattitude
        lon (float):       Coordinate longitude

    Returns:
        dictionary containing the following values:
            'temp' (float):    Hourly Temperature in degrees F.
            'hum' (float):     Hourly % humidity.
            'precip' (float):  Hourly inches of precip.
            'dew' (float):     Hourly dewpoint in degrees F.
            'wind' (float):    Hourly average windspeed in mph.
            'gusts' (float):   Hourly max windspeed in mph.
            'cloud' (float):   Hourly % cloud cover.
    """
    utc_dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    d_str = utc_dt.strftime("%Y-%m-%d")
    dt_str = utc_dt.strftime("%Y-%m-%dT%H:%M")
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&start_date={d_str}&end_date={d_str}&start_hour={dt_str}&end_hour={dt_str}&hourly=temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,wind_speed_10m,wind_gusts_10m,cloud_cover&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
    res = None
    try:
        res = requests.get(url, timeout=(3.05, 10)).json()
        if not res or not 'hourly' in res:
            raise ValueError("open-meteo.com() missing expected response.")
        hourly = res['hourly']
        temp = hourly.get('temperature_2m',[0.0])[0]
        hum = hourly.get('relative_humidity_2m', [0.0])[0]
        dew = hourly.get('dew_point_2m', [0.0])[0]
        precip = hourly.get('precipitation',[0.0])[0]
        wind = hourly.get('wind_speed_10m', [0.0])[0]
        gusts = hourly.get('wind_gusts_10m', [0.0])[0]
        cloud = hourly.get('cloud_cover', [0.0])[0]
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Exception in get_weather(): Failed to get weather "
                     f"for {lat},{lon}@UTC:{dt_str}\nurl:{url}\nres:{res}\n"
                     f"Details:{error_details}")
        temp=0.0; hum=0.0; precip=0.0; dew=0.0; wind=0.0; gusts=0.0; cloud=0.0

    return {"temp":temp, "hum":hum, "precip":precip, "dew":dew,
            "wind":wind, "gusts":gusts, "cloud":cloud}

def filter_outlier_coords(coords, edit_comments):
    """ Process a list of geojson coordinates to remove any outliers.
        Some remote id modules (ahem... Autel) do not care if they spit out 
        garbage coords, so try to filter the worst offenders out.  The returned
        coordinate array omits altitude and timestamp fields but will otherwise
        sequentially match the input array with any outliers tossed out.

    Args:
        coords [[
            lon (float),
            lat (float),
            alt (float),
            ts  (int)
         ]]
         edit_comments # list of any edits that are made.
    
    Returns:
        coords [[
            lat (float),
            lon (float)
        ]]
    """
    # Use Interquartile Range method to compute avg lat,lon sans any outliers:
    lat_list = []; lon_list = []
    for i in range(len(coords)):
      lat_list.append(float(coords[i][1]))
      lon_list.append(float(coords[i][0]))
    min_lat = np.percentile(lat_list, 2)
    max_lat = np.percentile(lat_list, 98)
    iqr_lat = max_lat - min_lat
    min_lon = np.percentile(lon_list, 2)
    max_lon = np.percentile(lon_list, 98)
    iqr_lon = max_lon - min_lon
    lower_lat = math.fabs(min_lat) - 1.5 * iqr_lat
    upper_lat = math.fabs(max_lat) + 1.5 * iqr_lat
    lower_lon = math.fabs(min_lon) - 1.5 * iqr_lon
    upper_lon = math.fabs(max_lon) + 1.5 * iqr_lon
    results = []
    for i in range(len(lat_list)):
        lat = lat_list[i]; lon = lon_list[i] 
        if (lower_lat <= math.fabs(lat) <= upper_lat) and (lower_lon <= math.fabs(lon) <= upper_lon):
            results.append([lat,lon])
        else:
            latpfx = ""; lonpfx = ""
            if lat < 0:
                latpfx = "-" 
            if lon < 0:
                lonpfx = "-"
            edit_comments.append(f"filter_outliers(): ignoring: {i}:!{lower_lat}<={lat}<={upper_lat},{lower_lon}<={lon}<={upper_lon}")
    return results

def compute_distance(coords):
    """ Process/filter geojson coords.
    Args:
        coords [[
            lat (float),
            lon (float)
         ]]
    
    Returns
          distance_mi (float)
    """
    total_dist_km = 0.0
    for i in range(len(coords) - 1):
        # Haversine Formula
        lon1, lat1 = float(coords[i][0]), float(coords[i][1])
        lon2, lat2 = float(coords[i+1][0]), float(coords[i+1][1])
        radius = 6371 # Earth radius in km
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        if (dlat != 0 and dlon != 0):
            a = (math.sin(dlat / 2) * math.sin(dlat / 2) +
                 math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
                 math.sin(dlon / 2) * math.sin(dlon / 2))
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            total_dist_km += radius * c
        
    # Convert km to miles w/~50' resolution:
    return round(total_dist_km * 0.621371, 2)

async def authenticate_tracker_token(token: Optional[str]) -> bool:
    valid, _credential = await authenticate_tracker_session(token)
    return valid


async def authenticate_tracker_session(
    token: Optional[str],
) -> tuple[bool, Optional[DeviceCredentialRecord]]:
    normalized = _normalize_tracker_token(token)
    if control_plane_store is None or not normalized.startswith("r2c_dev_"):
        return False, None
    credential = await control_plane_store.authenticate_device_token(normalized)
    return credential is not None, credential


async def get_api_key(
    header_value: str = Depends(api_key_header),
    functionality_release: Annotated[
        Optional[int], Header(alias="X-R2C-Functionality-Release")
    ] = None,
) -> Optional[DeviceCredentialRecord]:
    authenticated, credential = await authenticate_tracker_session(header_value)
    if authenticated:
        if R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE > 0 and (
            functionality_release is None
            or functionality_release < R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE
        ):
            raise HTTPException(
                status_code=status.HTTP_426_UPGRADE_REQUIRED,
                detail={
                    "code": "upgrade_required",
                    "minimum_functionality_release": (
                        R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE
                    ),
                    "message": "Upgrade RID2Caltopo to restore Tracker access.",
                },
                headers={"Upgrade": "RID2Caltopo"},
            )
        return credential
    normalized = _normalize_tracker_token(header_value)
    if control_plane_store is not None:
        challenge = await control_plane_store.device_reauthentication_challenge(
            normalized
        )
        if challenge is not None:
            credential_record, designator = challenge
            reauthentication_url = control_plane_tokens.device_reauthentication_url(
                credential_id=credential_record.id,
                organization_id=credential_record.organization_id,
                designator=designator,
                requested_at=credential_record.reauth_requested_at.isoformat(),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "reauthentication_required",
                    "clear_managed_configuration": False,
                    "reauthentication_url": reauthentication_url,
                    "message": (
                        "This device must reauthenticate before Tracker access "
                        "can be restored."
                    ),
                },
            )
    raise HTTPException(
        status_code=HTTP_403_FORBIDDEN,
        detail="Could not validate credentials",
    )


async def meter_organization_usage(
    credential: Optional[DeviceCredentialRecord],
    **increments,
) -> None:
    """Record delayed aggregate usage without affecting the client request."""
    if credential is None or control_plane_store is None:
        return
    try:
        await control_plane_store.increment_daily_usage(
            organization_id=credential.organization_id,
            **increments,
        )
    except Exception:
        logger.exception(
            "Unable to record aggregate organization usage: organization=%s",
            credential.organization_id,
        )


async def meter_organization_usage_by_id(
    organization_id: str,
    **increments,
) -> None:
    """Record aggregate usage when an authenticated organization user owns it."""
    if not organization_id or control_plane_store is None:
        return
    try:
        await control_plane_store.increment_daily_usage(
            organization_id=organization_id,
            **increments,
        )
    except Exception:
        logger.exception(
            "Unable to record aggregate organization usage: organization=%s",
            organization_id,
        )

async def archive_flight_log(
    title,
    flight_timestamp,
    geojson_data,
    flight_id: int,
    organization_designator: Optional[str] = None,
):
    relpath = archive_relpath_for_flight(
        flight_id,
        title,
        flight_timestamp,
        organization_designator,
    )
    target_directory = os.path.join(BASE_LOG_DIRECTORY, os.path.dirname(relpath))
    os.makedirs(target_directory, exist_ok=True)

    filepath = os.path.join(BASE_LOG_DIRECTORY, relpath)
    with open(filepath, 'w') as f:
        json.dump(geojson_data, f, indent=2)

    print(f"Flight log saved to: {filepath}")
    return relpath, filepath


async def extract_flight_inputs_from_geojson(data: dict):
    start_ts, end_ts = None, None
    prop = None
    coordinate_list = None

    for feature in data.get("features", []):
        if start_ts:
            raise HTTPException(400, "Only one track supported per log file.")
        prop = feature.get("properties")
        geometry = feature.get("geometry")
        if not geometry or geometry.get("type") != "LineString" or not geometry.get("coordinates"):
            continue
        coordinate_list = geometry["coordinates"]
        for coord in coordinate_list:
            if len(coord) >= 4:
                if not start_ts:
                    start_ts = int(coord[3])
                end_ts = int(coord[3])

    if not start_ts:
        raise HTTPException(400, "No LineString coordinate timestamps found.")
    if not prop:
        raise HTTPException(400, "No properties found.")

    start_ts_sec = round(start_ts / 1000.0, 2)
    end_ts_sec = round(end_ts / 1000.0, 2)
    duration_sec = end_ts_sec - start_ts_sec
    if duration_sec < 60:
        raise HTTPException(400, f"{duration_sec} second flight is too brief.")

    spec = parse_prop(prop)
    distance = spec.get('distance_mi')
    title = prop.get('title')

    processing_comments = []
    coords = filter_outlier_coords(coordinate_list, processing_comments)
    if not coords:
        raise HTTPException(status_code=409, detail=f"No valid coordinates in {title}")

    start_lat, start_lng = coords[0][0], coords[0][1]
    filter_count = len(coordinate_list) - len(coords)
    if filter_count > 0:
        processing_comments.append(f"ignoring {filter_count} outlier coordinates from {title}.   Start:{start_lat},{start_lng}")

    if not distance or filter_count > 0:
        distance = compute_distance(coords)
    if distance < 0.1:
        raise HTTPException(400, f"{distance} mi flight is too brief.")

    start_time = datetime.fromtimestamp(start_ts_sec, tz=timezone.utc).replace(tzinfo=None)
    localized_start_time = localize_flight_time(start_time, start_lat, start_lng)
    end_time = datetime.fromtimestamp(end_ts_sec, tz=timezone.utc).replace(tzinfo=None)

    if int(start_time.strftime("%Y")) == 1970:
        raise HTTPException(
            400,
            "Coordinate timestamps are likely straight from a UAS Remote ID msg."
            "They need to be converted to current UTC timestamps by the tool "
            "that is being used to extract them before reporting to a geo-json file."
        )

    return {
        "title": title,
        "spec": spec,
        "distance": distance,
        "start_time": start_time,
        "end_time": end_time,
        "localized_start_time": localized_start_time,
        "start_lat": start_lat,
        "start_lng": start_lng,
        "start_ts_sec": start_ts_sec,
        "duration_hrs": round((end_ts_sec - start_ts_sec) / 3600.0, 3),
        "processing_comments": processing_comments,
    }


async def create_flight_and_archive(
    db: AsyncSession,
    data: dict,
    flight_inputs: dict,
    credential: Optional[DeviceCredentialRecord] = None,
):
    spec = flight_inputs["spec"]
    title = flight_inputs["title"]
    start_time = flight_inputs["start_time"]
    end_time = flight_inputs["end_time"]
    remote_id = normalize_remote_id(spec.get('rid'))

    result = await find_overlap(
        db,
        start_time,
        end_time,
        remote_id=remote_id,
        sar_id=spec['sar_id'],
        organization_id=(credential.organization_id if credential else None),
    )
    existing = result.scalars().first()
    if existing:
        overlap_identity = remote_id if remote_id else spec['sar_id']
        raise HTTPException(
            status_code=409,
            detail=f"Conflict: This log overlaps with existing entry {existing.id} for {overlap_identity}"
        )

    timeofday_str = get_time_of_day(flight_inputs["start_ts_sec"], flight_inputs["start_lat"], flight_inputs["start_lng"])
    weather = get_weather(flight_inputs["start_ts_sec"], flight_inputs["start_lat"], flight_inputs["start_lng"])

    data['r2c-tracker'] = flight_inputs["processing_comments"]
    new_flight = Flight(
        organization_id=(credential.organization_id if credential else None),
        sar_id=spec['sar_id'].upper(),
        remote_id=remote_id,
        start_time=start_time,
        end_time=end_time,
        hours=flight_inputs["duration_hrs"],
        start_lat=flight_inputs["start_lat"],
        start_lng=flight_inputs["start_lng"],
        incident=spec['incident'],
        op_period=spec['op_period'],
        uas=spec['uas'].lower(),
        map_id=spec['map_id'].upper(),
        temp_f=weather['temp'],
        rhum_pct=weather['hum'],
        dewpt_f=weather['dew'],
        precip_in=weather['precip'],
        wind_mph=weather['wind'],
        gusts_mph=weather['gusts'],
        cloudcvr_pct=weather['cloud'],
        timeofday=timeofday_str,
        distance_mi=flight_inputs["distance"],
    )
    db.add(new_flight)
    await db.flush()

    archive_relpath, archive_path = await archive_flight_log(
        title,
        flight_inputs["localized_start_time"],
        data,
        new_flight.id,
        credential.designator if credential else None,
    )
    new_flight.archive_relpath = archive_relpath
    return new_flight, archive_path


async def create_imported_flight_and_archive(
        db: AsyncSession,
        data: dict,
        flight_inputs: dict,
        organization_id: Optional[str] = None,
        organization_designator: Optional[str] = None):
    spec = flight_inputs["spec"]
    remote_id = normalize_remote_id(spec.get('rid'))
    timeofday_str = get_time_of_day(
        flight_inputs["start_ts_sec"],
        flight_inputs["start_lat"],
        flight_inputs["start_lng"],
    )

    data['r2c-tracker'] = flight_inputs["processing_comments"]
    new_flight = Flight(
        organization_id=organization_id,
        sar_id=spec['sar_id'].upper(),
        remote_id=remote_id,
        start_time=flight_inputs["start_time"],
        end_time=flight_inputs["end_time"],
        hours=flight_inputs["duration_hrs"],
        start_lat=flight_inputs["start_lat"],
        start_lng=flight_inputs["start_lng"],
        incident=spec['incident'],
        op_period=spec['op_period'],
        uas=spec['uas'].lower(),
        map_id=spec['map_id'].upper(),
        temp_f=0.0,
        rhum_pct=0.0,
        dewpt_f=0.0,
        precip_in=0.0,
        wind_mph=0.0,
        gusts_mph=0.0,
        cloudcvr_pct=0.0,
        timeofday=timeofday_str,
        distance_mi=flight_inputs["distance"],
    )
    db.add(new_flight)
    await db.flush()

    archive_relpath, archive_path = await archive_flight_log(
        flight_inputs["title"],
        flight_inputs["localized_start_time"],
        data,
        new_flight.id,
        organization_designator,
    )
    new_flight.archive_relpath = archive_relpath
    return new_flight, archive_path


    
def format_datetime(value):
    if value is None:
        return ""
    return value.strftime('%d%b%y@%H:%M:%S-%Z')

def format_duration_hours(value: Optional[float]) -> str:
    if value is None:
        return "00:00:00"
    try:
        total_seconds = round(float(value) * 3600)
    except (TypeError, ValueError):
        return "00:00:00"
    total_seconds = max(int(total_seconds), 0)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def format_duration_seconds(value: Optional[int]) -> str:
    if value is None:
        return "—"
    try:
        total_seconds = max(int(value), 0)
    except (TypeError, ValueError):
        return "—"
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_media_bytes(value: Optional[int]) -> str:
    byte_count = max(0, int(value or 0))
    if byte_count >= 1_000_000:
        return f"{byte_count / 1_000_000:.1f} MB"
    if byte_count >= 1_000:
        return f"{byte_count / 1_000:.1f} KB"
    return f"{byte_count} B"

def datetime_from_format(fmtstr):
    return datetime.strptime(fmtstr, '%d%b%y@%H:%M:%S-%Z')

def to_iso_naive(dt):
    if dt is None:
        return ""
    return dt.isoformat()

def get_flashed_messages(request: Request):
    return request.session.pop("_messages") if "_messages" in request.session else []


def template_navigation(request, organization_designator=None):
    """Keep shared navigation inside the current authentication/data scope."""
    designator = str(organization_designator or "").strip().lower()
    if designator:
        return {
            "scope": "organization",
            "home_url": "/",
            "designator": designator,
        }
    try:
        path = request.url.path
    except AttributeError:
        path = ""
    if path.startswith("/platform-admin"):
        return {
            "scope": "platform_admin",
            "home_url": "/platform-admin/organizations",
            "designator": "",
        }
    if path in {"/", "/versions"}:
        return {
            "scope": "public",
            "home_url": "/",
            "designator": "",
        }
    return {"scope": "legacy", "home_url": "/", "designator": ""}


def organization_landing_path(organization, user) -> str:
    """Choose a useful first page without sending viewers into administration."""
    designator = organization.designator.lower()
    administrative_roles = {
        "organization_owner",
        "billing_admin",
        "config_admin",
        "user_admin",
        "records_admin",
    }
    if administrative_roles.intersection(user.roles):
        return f"/{designator}/admin"
    if "video_requester" in user.roles and "records_viewer" not in user.roles:
        return f"/{designator}/streams"
    return f"/{designator}"


templates.env.globals.update(
    get_flashed_messages=get_flashed_messages,
    template_navigation=template_navigation,
)
templates.env.globals["tracker_version"] = TRACKER_VERSION

def flash(request: Request, message: str, category: str = "info"):
    if "_messages" not in request.session:
        request.session["_messages"] = []
    request.session["_messages"].append({"message": message, "category": category})


def csrf_token(request: Request, namespace: str) -> str:
    key = f"_csrf_{namespace}"
    token = request.session.get(key)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[key] = token
    return token


def verify_csrf(request: Request, namespace: str, submitted: str) -> None:
    expected = request.session.get(f"_csrf_{namespace}", "")
    if (
        not submitted
        or not expected
        or not secrets.compare_digest(submitted, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired form token.",
        )


def admin_url(start_date: Optional[date] = None, end_date: Optional[date] = None, **extra_params) -> str:
    params = {}
    if start_date:
        params["start_date"] = start_date.isoformat()
    if end_date:
        params["end_date"] = end_date.isoformat()
    params.update({key: value for key, value in extra_params.items() if value is not None})
    return f"/admin?{urlencode(params)}" if params else "/admin"


def organization_flight_admin_url(
        designator: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        **extra_params) -> str:
    params = {}
    if start_date:
        params["start_date"] = start_date.isoformat()
    if end_date:
        params["end_date"] = end_date.isoformat()
    params.update(
        {key: value for key, value in extra_params.items() if value is not None}
    )
    base_url = f"/{designator.lower()}/admin/flights"
    return f"{base_url}?{urlencode(params)}" if params else base_url

def export_url(start_date: Optional[date] = None, end_date: Optional[date] = None) -> str:
    params = {}
    if start_date:
        params["start_date"] = start_date.isoformat()
    if end_date:
        params["end_date"] = end_date.isoformat()
    return f"/export?{urlencode(params)}" if params else "/export"


FILTER_TIMEZONE = ZoneInfo("America/Los_Angeles")

def local_date_bounds_to_utc(start_date: Optional[date] = None, end_date: Optional[date] = None):
    start_dt = None
    end_dt = None
    if start_date:
        start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=FILTER_TIMEZONE)
        start_dt = start_dt.astimezone(UTC).replace(tzinfo=None)
    if end_date:
        end_dt = datetime.combine(end_date, datetime.max.time(), tzinfo=FILTER_TIMEZONE)
        end_dt = end_dt.astimezone(UTC).replace(tzinfo=None)
    return start_dt, end_dt

def apply_date_filter(stmt, start_date: Optional[date] = None, end_date: Optional[date] = None):
    start_dt, end_dt = local_date_bounds_to_utc(start_date, end_date)
    if start_dt:
        stmt = stmt.where(Flight.start_time >= start_dt)
    if end_dt:
        stmt = stmt.where(Flight.start_time <= end_dt)
    return stmt


def parse_admin_batch_form(form_data):
    action = str(form_data.get("action", "save")).strip() or "save"

    flight_ids = []
    seen_flight_ids = set()
    for raw_flight_id in form_data.getlist("flight_ids"):
        try:
            flight_id = int(raw_flight_id)
        except (TypeError, ValueError):
            continue
        if flight_id in seen_flight_ids:
            continue
        seen_flight_ids.add(flight_id)
        flight_ids.append(flight_id)

    delete_ids = set()
    for raw_delete_id in form_data.getlist("delete_ids"):
        try:
            delete_id = int(raw_delete_id)
        except (TypeError, ValueError):
            continue
        if delete_id in seen_flight_ids:
            delete_ids.add(delete_id)

    updates = {}
    for flight_id in flight_ids:
        updates[flight_id] = {
            "sar_id": str(form_data.get(f"sar_id_{flight_id}", "")).upper().strip(),
            "uas": str(form_data.get(f"uas_{flight_id}", "")).lower().strip(),
        }

    return action, flight_ids, delete_ids, updates


def normalize_csv_value(value, default=""):
    if value is None:
        return default
    value = str(value).strip()
    return value if value else default


def normalize_remote_id(value, default=""):
    return normalize_csv_value(value, default).upper()


def resolve_overlap_identity(remote_id: Optional[str], sar_id: Optional[str]) -> tuple[str, str, bool]:
    normalized_remote_id = normalize_remote_id(remote_id)
    normalized_sar_id = normalize_csv_value(sar_id).upper()
    fallback_to_sar = bool(normalized_remote_id and normalized_sar_id)
    return normalized_remote_id, normalized_sar_id, fallback_to_sar


def parse_csv_float(value, default=0.0):
    try:
        return float(normalize_csv_value(value, default))
    except (TypeError, ValueError):
        return default


def normalize_match_datetime(dt):
    if dt is None:
        return None
    return dt.replace(microsecond=0)


def datetime_match_within_seconds(dt_a, dt_b, tolerance_seconds=2):
    if dt_a is None or dt_b is None:
        return False
    return abs((normalize_match_datetime(dt_a) - normalize_match_datetime(dt_b)).total_seconds()) <= tolerance_seconds


def coordinates_match(lat_a, lng_a, lat_b, lng_b, tolerance=0.0005):
    return abs(lat_a - lat_b) <= tolerance and abs(lng_a - lng_b) <= tolerance


def archive_filename_for_flight(flight_id: int, title: str, flight_timestamp: datetime) -> str:
    safe_title = title or "no_title"
    filename_timestamp = flight_timestamp.strftime("%d%b%Y_%H%M%S_%Z")
    return f"flightlog_{flight_id}_{filename_timestamp}-{safe_title}.json"


def archive_relpath_for_flight(
    flight_id: int,
    title: str,
    flight_timestamp: datetime,
    organization_designator: Optional[str] = None,
) -> str:
    year = flight_timestamp.strftime("%Y")
    month = flight_timestamp.strftime("%m")
    filename = archive_filename_for_flight(flight_id, title, flight_timestamp)
    if organization_designator:
        safe_designator = re.sub(
            r"[^a-z0-9]",
            "",
            organization_designator.strip().lower(),
        )
        if not safe_designator:
            raise ValueError("Organization designator is invalid for log storage.")
        return os.path.join("organizations", safe_designator, year, month, filename)
    return os.path.join(year, month, filename)


def parse_flight_id_from_archive_filename(filename: str) -> Optional[int]:
    match = re.match(r"flightlog_(\d+)_", filename or "")
    if not match:
        return None
    return int(match.group(1))


TF = TimezoneFinder()
def localize_flight_time(dt, lat, lng):
    if not dt or lat is None or lng is None:
        return dt

    dt_utc = dt.replace(tzinfo=timezone.utc)

    # Returns a string like 'America/Los_Angeles' or None
    tz_name = TF.timezone_at(lat=lat, lng=lng)

    # Convert to the local timezone
    if tz_name:
        local_dt = dt_utc.astimezone(ZoneInfo(tz_name))
        return local_dt
    return dt_utc


async def find_overlap(
    db,
    start_time,
    end_time,
    remote_id: Optional[str] = None,
    sar_id: Optional[str] = None,
    organization_id: Optional[str] = None,
):
    # Make sure a flight doesn't overlap an existing flight for the same aircraft identity.
    remote_id, sar_id, fallback_to_sar = resolve_overlap_identity(remote_id, sar_id)

    identity_filters = []
    if remote_id:
        identity_filters.append(Flight.remote_id == remote_id)
        if fallback_to_sar:
            identity_filters.append(and_(
                or_(Flight.remote_id.is_(None), Flight.remote_id == ""),
                Flight.sar_id == sar_id
            ))
    elif sar_id:
        identity_filters.append(Flight.sar_id == sar_id)
    else:
        return await db.execute(select(Flight).where(text("1 = 0")))

    organization_filter = (
        Flight.organization_id == organization_id
        if organization_id
        else Flight.organization_id.is_(None)
    )
    stmt = select(Flight).filter(organization_filter).filter(or_(*identity_filters)).filter(
        or_(
            # New start falls inside an existing flight
            and_(Flight.start_time <= start_time, Flight.end_time > start_time),
            # New end falls inside an existing flight
            and_(Flight.start_time < end_time, Flight.end_time >= end_time),
            # New flight completely swallows an existing flight
            and_(Flight.start_time >= start_time, Flight.end_time <= end_time)
        )
    )
    return await db.execute(stmt)



templates.env.filters["localize_flight_time"] = localize_flight_time
templates.env.filters["fmt_datetime"] = format_datetime
templates.env.filters["duration_hms"] = format_duration_hours
templates.env.filters["duration_clock"] = format_duration_seconds
templates.env.filters["media_bytes"] = format_media_bytes

# --- ROUTES ---
@app.get("/faa/notams", response_class=Response)
async def faa_notams(
        latitude: float = Query(...),
        longitude: float = Query(...),
        radius: float = Query(...),
        lastUpdatedDate: Optional[str] = Query(None),
        credential: Optional[DeviceCredentialRecord] = Depends(get_api_key)):
    """
    Return nearby FAA NOTAM GeoJSON without exposing FAA credentials to R2C.

    Full queries use a small, safety-expanded geographic cache. Incremental
    queries containing lastUpdatedDate bypass the cache.
    """
    try:
        result = await faa_notam_proxy.fetch_notams(
            latitude=latitude,
            longitude=longitude,
            radius_nm=radius,
            last_updated_date=lastUpdatedDate,
        )
    except FaaProxyError as exc:
        logger.warning("FAA proxy request failed: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    response_bytes = (
        len(result.body)
        if isinstance(result.body, bytes)
        else len(str(result.body).encode("utf-8"))
    )
    await meter_organization_usage(
        credential,
        compute_units=Decimal("1"),
        network_bytes=response_bytes,
        faa_proxy_requests=1,
    )
    return Response(
        content=result.body,
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "X-R2C-FAA-Cache": result.cache_status,
            "X-R2C-FAA-Cache-Age": str(result.age_seconds),
        },
    )


def require_scoped_upload_credential(
        designator: str,
        credential: Optional[DeviceCredentialRecord],
) -> DeviceCredentialRecord:
    """Bind an organization upload URL to its issued device credential."""
    normalized = designator.strip().lower()
    if credential is None or credential.designator.strip().lower() != normalized:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail="Device credential does not belong to this organization",
        )
    return credential


ORG_CONFIG_MAX_BYTES = 256 * 1024
ORG_CONFIG_MAX_DRONES = 500


def validated_organization_config_snapshot(snapshot: object) -> tuple[dict, str]:
    if not isinstance(snapshot, dict):
        raise ValueError("Organization configuration must be a JSON object.")
    allowed = {
        "configSchemaVersion", "sourcePlatform", "sourceAppVersion",
        "sourceAppBuild", "organizationCaltopoEnc", "mutualAidCaltopoEnc",
        "droneSpecs",
    }
    if set(snapshot) - allowed:
        raise ValueError("Organization configuration contains unsupported fields.")
    if snapshot.get("configSchemaVersion") != 1:
        raise ValueError("Unsupported organization configuration schema version.")
    platform = str(snapshot.get("sourcePlatform", "")).strip().lower()
    if platform not in {"android", "ios", "ipados"}:
        raise ValueError("Invalid source platform.")
    app_version = str(snapshot.get("sourceAppVersion", "")).strip()
    app_build = snapshot.get("sourceAppBuild")
    if not app_version or len(app_version) > 64:
        raise ValueError("Invalid source app version.")
    if not isinstance(app_build, int) or isinstance(app_build, bool) or app_build < 0:
        raise ValueError("Invalid source app build.")
    org_enc = snapshot.get("organizationCaltopoEnc")
    ma_enc = snapshot.get("mutualAidCaltopoEnc", "")
    if not isinstance(org_enc, str) or not org_enc or len(org_enc) > 65_536:
        raise ValueError("Organization CalTopo configuration is missing or invalid.")
    if not isinstance(ma_enc, str) or len(ma_enc) > 65_536:
        raise ValueError("Mutual-aid CalTopo configuration is invalid.")
    drones = snapshot.get("droneSpecs")
    if not isinstance(drones, list) or len(drones) > ORG_CONFIG_MAX_DRONES:
        raise ValueError("Drone specifications are missing or exceed the limit.")
    normalized_drones = []
    remote_ids = set()
    allowed_drone_fields = {"remoteId", "mappedId", "org", "model", "owner"}
    for item in drones:
        if not isinstance(item, dict) or set(item) - allowed_drone_fields:
            raise ValueError("A drone specification contains unsupported fields.")
        normalized = {
            key: str(item.get(key, "")).strip()
            for key in ("remoteId", "mappedId", "org", "model", "owner")
        }
        remote_id = normalized["remoteId"]
        if not remote_id or len(remote_id) > 160 or remote_id.casefold() in remote_ids:
            raise ValueError("Drone remote IDs must be present and unique.")
        if any(len(value) > 200 for value in normalized.values()):
            raise ValueError("A drone specification field is too long.")
        remote_ids.add(remote_id.casefold())
        normalized_drones.append(normalized)
    normalized_snapshot = {
        "configSchemaVersion": 1,
        "sourcePlatform": platform,
        "sourceAppVersion": app_version,
        "sourceAppBuild": app_build,
        "organizationCaltopoEnc": org_enc,
        "mutualAidCaltopoEnc": ma_enc,
        "droneSpecs": sorted(normalized_drones, key=lambda item: item["remoteId"].casefold()),
    }
    encoded = json.dumps(normalized_snapshot, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > ORG_CONFIG_MAX_BYTES:
        raise ValueError("Organization configuration exceeds the 256 KiB limit.")
    if "r2c_dev_" in encoded:
        raise ValueError("Organization configuration contains a device credential.")
    return normalized_snapshot, encoded


def organization_config_diff(current: Optional[dict], proposed: dict) -> dict:
    current = current or {}
    def credential_status(field: str) -> str:
        old, new = str(current.get(field, "")), str(proposed.get(field, ""))
        if not old and new:
            return "added"
        if old and not new:
            return "removed"
        return "changed" if old != new else "unchanged"
    old_drones = {item["remoteId"].casefold(): item for item in current.get("droneSpecs", [])}
    new_drones = {item["remoteId"].casefold(): item for item in proposed["droneSpecs"]}
    return {
        "organizationCaltopo": credential_status("organizationCaltopoEnc"),
        "mutualAidCaltopo": credential_status("mutualAidCaltopoEnc"),
        "addedDrones": [new_drones[key] for key in sorted(new_drones.keys() - old_drones.keys())],
        "removedDrones": [old_drones[key] for key in sorted(old_drones.keys() - new_drones.keys())],
        "changedDrones": [
            {"before": old_drones[key], "after": new_drones[key]}
            for key in sorted(old_drones.keys() & new_drones.keys())
            if old_drones[key] != new_drones[key]
        ],
    }


@app.get("/{designator}/api/v1/organization-config/current")
async def current_organization_config(
    designator: str,
    credential: Optional[DeviceCredentialRecord] = Depends(get_api_key),
):
    credential = require_scoped_upload_credential(designator, credential)
    release = await control_plane_store.get_current_organization_config_release(
        credential.organization_id
    )
    if release is None:
        return Response(status_code=204, headers={"Cache-Control": "private, no-store"})
    return JSONResponse(
        {"versionMs": release.version_ms, "config": release.snapshot},
        headers={"Cache-Control": "private, no-store"},
    )


@app.put("/{designator}/upload")
async def upload(
        request: Request,
        designator: str,
        db: AsyncSession = Depends(get_db),
        credential: Optional[DeviceCredentialRecord] = Depends(get_api_key)):
    credential = require_scoped_upload_credential(designator, credential)
    raw_body = await request.body()
    try:
        data = await request.json()
        if not data:
            raise ValueError("No data received in payload")
        sar_id = data.get("sar_id", "unknown")

    except ValueError as ve:
        logger.warning(f"Validation Error: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Exception in /upload:\n{error_details}")
        raise HTTPException(status_code=500, detail=f"Server Error: {str(e)}")
    flight_inputs = await extract_flight_inputs_from_geojson(data)
    async with serialized_flight_submission(
            flight_inputs["spec"].get("rid"),
            flight_inputs["spec"].get("sar_id"),
    ):
        new_flight, archive_path = await create_flight_and_archive(
            db,
            data,
            flight_inputs,
            credential,
        )
        await db.commit()

    archive_size = os.path.getsize(archive_path) if os.path.exists(archive_path) else 0
    await meter_organization_usage(
        credential,
        compute_units=Decimal("1"),
        database_units=Decimal("1"),
        network_bytes=len(raw_body),
        storage_byte_days=archive_size,
    )
    return {"status": "Logged",
            "hours": flight_inputs["duration_hrs"],
            "timeofday": new_flight.timeofday,
            "distance_mi": flight_inputs["distance"],
            "spec": flight_inputs["spec"],
            "weather": {
                "temp": new_flight.temp_f,
                "hum": new_flight.rhum_pct,
                "dew": new_flight.dewpt_f,
                "precip": new_flight.precip_in,
                "wind": new_flight.wind_mph,
                "gusts": new_flight.gusts_mph,
                "cloud": new_flight.cloudcvr_pct,
            }
            }

ANDROID_APP_LINK_CERTIFICATES = (
    # Google Play app-signing certificate. This is the certificate installed
    # on operator devices, not the upload certificate used for AAB submission.
    "21:88:EC:89:72:29:2D:83:97:07:EB:DE:09:2B:F8:C1:31:46:6C:93:37:89:BE:49:3D:3D:06:C0:F2:37:EB:3E",
    # Direct release APKs are signed with the protected upload key.
    "92:F8:E8:39:8B:4D:2B:85:BB:EB:1D:DF:15:B2:27:E4:4D:BC:AD:29:12:22:AF:58:1E:4A:38:6E:FD:2E:13:0A",
)
APPLE_APP_IDENTIFIER = "94UV79S6LR.org.ncssar.RID2CaltopoApple"


@app.get("/.well-known/assetlinks.json", include_in_schema=False)
async def android_asset_links():
    return JSONResponse(
        content=[{
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "org.ncssar.rid2caltopo",
                "sha256_cert_fingerprints": list(ANDROID_APP_LINK_CERTIFICATES),
            },
        }],
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/.well-known/apple-app-site-association", include_in_schema=False)
async def apple_app_site_association():
    return JSONResponse(
        content={
            "applinks": {
                "apps": [],
                "details": [{
                    "appID": APPLE_APP_IDENTIFIER,
                    "paths": ["/*/enroll"],
                }],
            },
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/", response_class=HTMLResponse)
async def organization_directory(
        request: Request,
        response: Response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    organizations = ()
    authorized_organizations = ()
    current_organization_designator = ""
    organization_identity_name = ""
    if control_plane_store is not None:
        all_organizations = await control_plane_store.list_organizations()
        organization_by_id = {
            organization.id: organization
            for organization in all_organizations
            if organization.lifecycle_state != "archived"
        }
        user_id = request.session.get("organization_user_id")
        session_designator = request.session.get("organization_designator")
        current_user = (
            await control_plane_store.get_user(user_id)
            if isinstance(user_id, str)
            else None
        )
        current_organization = organization_by_id.get(
            current_user.organization_id if current_user is not None else ""
        )
        if (
            current_user is not None
            and current_user.state == "active"
            and current_organization is not None
            and session_designator == current_organization.designator
        ):
            memberships = await organization_session_memberships(
                request, current_user
            )
            authorized_ids = {membership.organization_id for membership in memberships}
            authorized_organizations = tuple(
                sorted(
                    (
                        organization_by_id[organization_id]
                        for organization_id in authorized_ids
                        if organization_id in organization_by_id
                    ),
                    key=lambda organization: (
                        organization.legal_name.casefold(),
                        organization.designator,
                    ),
                )
            )
            current_organization_designator = current_organization.designator
            organization_identity_name = current_user.display_name
        elif user_id or session_designator:
            request.session.pop("organization_user_id", None)
            request.session.pop("organization_designator", None)

        authorized_ids = {
            organization.id for organization in authorized_organizations
        }
        organizations = tuple(
            sorted(
                (
                    organization
                    for organization in all_organizations
                    if (
                        organization.lifecycle_state != "archived"
                        and organization.records_visibility == "public"
                        and organization.id not in authorized_ids
                    )
                ),
                key=lambda organization: (
                    organization.legal_name.casefold(),
                    organization.designator,
                ),
            )
        )
    return templates.TemplateResponse(
        request=request,
        name="organization_directory.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "organizations": organizations,
            "authorized_organizations": authorized_organizations,
            "current_organization_designator": current_organization_designator,
            "organization_identity_name": organization_identity_name,
            "directory_identity_name": organization_identity_name or "Guest",
            "organization_select_csrf_token": csrf_token(
                request,
                "organization_select",
            ),
        },
    )


@app.post("/organizations/select", include_in_schema=False)
async def select_authorized_organization(
        request: Request,
        form_token: Annotated[str, Form()],
        designator: Annotated[str, Form()]):
    verify_csrf(request, "organization_select", form_token)
    session_designator = request.session.get("organization_designator")
    if not isinstance(session_designator, str):
        raise HTTPException(status_code=401, detail="Organization login required.")
    _current_organization, current_user = await require_organization_user(
        request,
        session_designator,
    )
    organization = await control_plane_store.get_organization(designator)
    memberships = await organization_session_memberships(request, current_user)
    membership = next(
        (
            candidate
            for candidate in memberships
            if organization is not None
            and candidate.organization_id == organization.id
        ),
        None,
    )
    if organization is None or membership is None:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized for that organization.",
        )
    request.session["organization_user_id"] = membership.id
    request.session["organization_designator"] = organization.designator
    return RedirectResponse(
        organization_landing_path(organization, membership),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/login", include_in_schema=False)
async def organization_login_redirect(organization: str):
    """Resolve a user-entered organization code without listing private tenants."""
    if control_plane_store is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        record = await control_plane_store.get_organization(organization)
    except InvalidOrganizationError:
        record = None
    if record is None or record.lifecycle_state == "archived":
        raise HTTPException(status_code=404, detail="Organization not found.")
    return RedirectResponse(
        f"/{record.designator.lower()}/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/managed-access-requests", include_in_schema=False)
async def managed_access_request_ingest(
        request: Request,
        requester_name: Annotated[str, Form()],
        requester_email: Annotated[str, Form()],
        organization_name: Annotated[str, Form()],
        designator: Annotated[str, Form()],
        source_host: Annotated[str, Form()],
        terms_version: Annotated[str, Form()],
        terms_acknowledged: Annotated[str, Form()],
        requester_phone: Annotated[str, Form()] = ""):
    if not MANAGED_REQUEST_INGEST_KEY:
        raise HTTPException(status_code=503, detail="Request intake is not configured.")
    authorization = request.headers.get("authorization", "")
    expected = f"Bearer {MANAGED_REQUEST_INGEST_KEY}"
    if not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=403, detail="Request intake authorization failed.")
    if control_plane_store is None:
        raise HTTPException(status_code=503, detail="Request storage is not configured.")
    try:
        record = await control_plane_store.create_managed_access_request(
            requester_name=requester_name,
            requester_email=requester_email,
            requester_phone=requester_phone,
            organization_name=organization_name,
            designator=designator,
            source_host=source_host,
            terms_acknowledged=terms_acknowledged.strip().lower()
            in {"1", "true", "yes", "on"},
            terms_version=terms_version,
        )
    except (ControlPlaneError, InvalidOrganizationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "received", "request_id": record.id}


async def render_public_dashboard(
        request: Request,
        response: Response,
        db: AsyncSession,
        start_date: Optional[date],
        end_date: Optional[date],
        organization=None):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"

    # Base query:
    organization_filter = (
        Flight.organization_id == organization.id
        if organization is not None
        else Flight.organization_id.is_(None)
    )
    stmt = apply_date_filter(
        select(Flight).where(organization_filter),
        start_date,
        end_date,
    )

    # Group by pilot, sum hours
    subq_totals = stmt.with_only_columns(
        Flight.sar_id,
        func.sum(Flight.hours).label("total_hours"),
        func.sum(Flight.distance_mi).label("total_miles"),
        func.max(Flight.start_time).label("last_active")
    ).group_by(Flight.sar_id).subquery()

    # Window subquery to find the single latest record per sar_id
    # This is how we get lt/lng without joining or grouping complications
    latest_flights_subq = stmt.with_only_columns(
        Flight.sar_id,
        Flight.start_lat,
        Flight.start_lng,
        func.row_number().over(
            partition_by=Flight.sar_id,
            order_by=Flight.start_time.desc()
        ).label("rn")
    ).subquery()

    leaderboard_stmt = select(
        subq_totals.c.sar_id,
        subq_totals.c.total_hours,
        subq_totals.c.total_miles,
        subq_totals.c.last_active,
        latest_flights_subq.c.start_lat,
        latest_flights_subq.c.start_lng
    ).join(
        latest_flights_subq,
        (subq_totals.c.sar_id == latest_flights_subq.c.sar_id) & (latest_flights_subq.c.rn == 1)
    ).order_by(desc(subq_totals.c.total_hours)).limit(10)
    leaderboard_result = await db.execute(leaderboard_stmt)
    leaderboard = leaderboard_result.all()

    flights_stmt = stmt.order_by(Flight.start_time.desc())
    if not start_date and not end_date:
        flights_stmt = flights_stmt.limit(25)
    flights_result = await db.execute(flights_stmt)
    flights = flights_result.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "flights": flights,
            "timezone" : ZoneInfo("America/Los_Angeles"),
            "leaderboard": leaderboard,
            "start_date" : start_date,
            "end_date" : end_date,
            "dashboard_url": (
                f"/{organization.designator.lower()}" if organization else "/"
            ),
            "organization_page_designator": (
                organization.designator if organization else None
            ),
            "organization_legal_name": (
                organization.legal_name if organization else None
            ),
            "organization_identity_name": (
                await organization_page_identity(request, organization.designator)
                if organization is not None
                else ""
            ),
        },
    )


@app.get("/versions", response_class=HTMLResponse)
async def version_history(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="versions.html",
        context={
            "request": request,
            "current_version": TRACKER_VERSION,
            "versions": load_recent_versions(),
        },
    )


@app.get("/livez")
async def liveness(response: Response):
    """Process-only probe; intentionally does not depend on external services."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return {"status": "ok", "version": TRACKER_VERSION}


@app.get("/readyz")
async def readiness(response: Response):
    """Confirm the revision can reach both persistent databases."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    checks = {"tracker_database": False, "control_plane_database": False}
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(select(1))
        checks["tracker_database"] = True
        if control_plane_store is None:
            checks["control_plane_database"] = True
        else:
            await control_plane_store.ping()
            checks["control_plane_database"] = True
    except Exception:
        logger.exception("Revision readiness check failed")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "version": TRACKER_VERSION, "checks": checks}
    return {"status": "ready", "version": TRACKER_VERSION, "checks": checks}


@app.post("/webhooks/app-store-connect", status_code=status.HTTP_204_NO_CONTENT)
async def app_store_connect_webhook(request: Request):
    if (
        not APP_STORE_CONNECT_WEBHOOK_SECRET
        or not TESTFLIGHT_FEEDBACK_EMAIL
        or control_plane_store is None
        or not platform_admin_email_sender.is_configured
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TestFlight feedback notifications are not configured.",
        )
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="App Store Connect webhook payload is too large.",
        )
    try:
        event = authenticate_app_store_connect_webhook(
            body,
            request.headers.get("x-apple-signature", ""),
            APP_STORE_CONNECT_WEBHOOK_SECRET,
        )
    except AppStoreConnectSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    except AppStoreConnectWebhookError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if event is None:
        try:
            await asyncio.to_thread(
                platform_admin_email_sender.send_testflight_webhook_test,
                recipient=TESTFLIGHT_FEEDBACK_EMAIL,
                app_name=TESTFLIGHT_APP_NAME,
            )
        except Exception as exc:
            logger.exception("TestFlight webhook test notification failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="TestFlight webhook test email could not be delivered.",
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    claim = await control_plane_store.claim_external_webhook_delivery(
        provider="app_store_connect",
        event_id=event.event_id,
        event_type=event.event_type,
        resource_type=event.resource_type,
        resource_id=event.feedback_id,
    )
    if claim != "claimed":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        await asyncio.to_thread(
            platform_admin_email_sender.send_testflight_feedback,
            recipient=TESTFLIGHT_FEEDBACK_EMAIL,
            app_name=TESTFLIGHT_APP_NAME,
            feedback_kind=event.feedback_kind,
            feedback_id=event.feedback_id,
            event_timestamp=(
                event.timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")
            ),
            app_store_connect_url=TESTFLIGHT_APP_STORE_CONNECT_URL,
        )
        await control_plane_store.mark_external_webhook_delivery_sent(
            provider="app_store_connect",
            event_id=event.event_id,
        )
    except Exception as exc:
        logger.exception("TestFlight feedback notification delivery failed")
        await control_plane_store.mark_external_webhook_delivery_failed(
            provider="app_store_connect",
            event_id=event.event_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TestFlight feedback notification could not be delivered.",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def require_deployment_gate_key(request: Request) -> None:
    if not DEPLOYMENT_GATE_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Deployment gate is not configured.",
        )
    authorization = request.headers.get("authorization", "")
    prefix = "Bearer "
    candidate = authorization[len(prefix):] if authorization.startswith(prefix) else ""
    if not candidate or not secrets.compare_digest(candidate, DEPLOYMENT_GATE_KEY):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def probe_flightlog_storage() -> None:
    """Perform a bounded write/read/delete check against the mounted archive store."""
    if not FLIGHTLOGS_STORAGE_REQUIRED:
        return
    if not os.path.isdir(BASE_LOG_DIRECTORY):
        raise RuntimeError("Flight-log storage mount is unavailable.")
    probe_path = os.path.join(
        BASE_LOG_DIRECTORY,
        f".r2c-release-probe-{secrets.token_hex(12)}",
    )
    payload = b"r2c-release-probe\n"
    try:
        with open(probe_path, "xb") as probe_file:
            probe_file.write(payload)
        with open(probe_path, "rb") as probe_file:
            if probe_file.read() != payload:
                raise RuntimeError("Flight-log storage probe read did not match its write.")
    finally:
        if os.path.exists(probe_path):
            os.unlink(probe_path)


@app.get("/deployment-readiness")
async def deployment_readiness(
        request: Request,
        response: Response,
        storage_probe: bool = Query(False)):
    """Fail closed when operational activity makes a revision switch unsafe."""
    require_deployment_gate_key(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    now = datetime.now(tz=UTC)
    now_ms = int(now.timestamp() * 1000)
    recent_zone_cutoff_ms = now_ms - (R2C_HEARTBEAT_SEC * 1000 * 2)
    async with AsyncSessionLocal() as session:
        active_zone_rows = (await session.scalars(
            select(R2CZoneState).where(
                R2CZoneState.last_seen_ms >= recent_zone_cutoff_ms,
                func.lower(R2CZoneState.connection_state).not_in(("idle", "disconnected")),
            )
        )).all()
    activity = {
        "local_coordination_connections": await r2c_hub.connection_count(),
        "recent_coordination_zones": len(active_zone_rows),
        "video_dashboard_connections": await organization_stream_event_hub.connection_count(),
        "active_video_streams": 0,
        "active_video_requests": 0,
    }
    activity_details = {
        "local_coordination_connections": (
            await r2c_hub.deployment_connection_details()
        ),
        "recent_coordination_zones": [
            {
                "organization_id": zone.organization_id,
                "device": zone.name,
                "map_id": zone.map_id,
                "zone_id": zone.zone_id,
                "app_version": zone.app_version,
                "app_version_code": zone.app_version_code,
                "connection_state": zone.connection_state,
                "last_seen_at": datetime.fromtimestamp(
                    zone.last_seen_ms / 1000,
                    tz=UTC,
                ).isoformat(),
            }
            for zone in active_zone_rows
        ],
        "video_dashboard_connections": (
            await organization_stream_event_hub.deployment_connection_details()
        ),
        "active_video_streams": [],
        "active_video_requests": [],
    }
    if control_plane_store is not None:
        activity.update(await control_plane_store.deployment_activity(now=now))
        activity_details.update(
            await control_plane_store.deployment_activity_details(now=now)
        )
    if storage_probe:
        await asyncio.to_thread(probe_flightlog_storage)
    reasons = [name for name, count in activity.items() if count > 0]
    return {
        "status": "idle" if not reasons else "active",
        "safe_to_deploy": not reasons,
        "version": TRACKER_VERSION,
        "checked_at": now.isoformat(),
        "activity": activity,
        "activity_details": activity_details,
        "storage_probe": "passed" if storage_probe else "not_requested",
        "reasons": reasons,
    }


@app.post("/deployment-test-fixture")
async def deployment_test_fixture(request: Request, response: Response):
    """Create an isolated authenticated fixture only in a staging clone."""
    if not RELEASE_STAGING_MODE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    require_deployment_gate_key(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if control_plane_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The staging control-plane database is unavailable.",
        )
    designator = "RELEASECHECK"
    try:
        existing = await control_plane_store.get_organization(designator)
    except InvalidOrganizationError:
        existing = None
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The staging fixture already exists. Refresh the staging "
                "database clones before starting another release."
            ),
        )
    organization = await control_plane_store.create_organization(
        legal_name="Release Check Search and Rescue",
        designator=designator,
        admin_name="Release Check Administrator",
        admin_email="release-check@example.invalid",
        postal_address="Isolated staging fixture",
        actor_id="release-staging",
        simulation=True,
    )
    owner = await control_plane_store.activate_owner(
        designator,
        organization.primary_admin_email,
        secrets.token_urlsafe(32),
    )
    campaign = await control_plane_store.create_enrollment_campaign(
        organization_id=organization.id,
        label="Automated staging release check",
        created_by_user_id=owner.id,
        expires_in_hours=1,
        max_redemptions=1,
    )
    credential = await control_plane_store.issue_device_credential(
        campaign_id=campaign.id,
        organization_id=organization.id,
        device_name="Staging Release Check",
        platform="android",
    )
    return {
        "designator": organization.designator.lower(),
        "device_token": credential.token,
        "expires_at": credential.expires_at.isoformat(),
    }


@app.get("/platform-admin/login", response_class=HTMLResponse)
async def platform_admin_login_page(
        request: Request,
        next: str = "/platform-admin/organizations"):
    identity, authoritative_user = await current_platform_admin_identity()
    existing_id = request.session.get("platform_admin_user_id")
    if isinstance(existing_id, str):
        existing = await control_plane_store.get_platform_admin(existing_id)
        if (
            existing is not None
            and existing.id == authoritative_user.id
            and request.session.get("platform_admin_identity_generation")
            == identity.generation
        ):
            return RedirectResponse(
                url="/platform-admin/organizations",
                status_code=status.HTTP_303_SEE_OTHER,
            )
    safe_next = (
        next
        if next.startswith("/platform-admin/") and not next.startswith("//")
        else "/platform-admin/organizations"
    )
    has_password = await control_plane_store.platform_admin_has_password(
        authoritative_user.id
    )
    return templates.TemplateResponse(
        request=request,
        name="platform_admin_login.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "csrf_token": csrf_token(request, "platform_admin_login"),
            "email_csrf_token": csrf_token(
                request,
                "platform_admin_setup_request",
            ),
            "next": safe_next,
            "has_password": has_password,
            "google_login_enabled": google_oidc_client.is_configured,
            "email_setup_enabled": platform_admin_email_sender.is_configured,
        },
    )


@app.post("/platform-admin/login")
async def platform_admin_login(
        request: Request,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        form_token: Annotated[str, Form()],
        next: Annotated[str, Form()] = "/platform-admin/organizations"):
    verify_csrf(request, "platform_admin_login", form_token)
    identity, authoritative_user = await current_platform_admin_identity()
    try:
        user = (
            await control_plane_store.authenticate_platform_admin(
                identity.email,
                password,
            )
            if secrets.compare_digest(email.strip().lower(), identity.email)
            else None
        )
    except InvalidOrganizationError:
        user = None
    if user is None:
        flash(request, "Incorrect email or password.", "warning")
        return RedirectResponse(
            url="/platform-admin/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    clear_platform_admin_session(request)
    if user.id != authoritative_user.id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform administrator identity changed during login.",
        )
    request.session["platform_admin_user_id"] = user.id
    request.session["platform_admin_identity_generation"] = identity.generation
    safe_next = (
        next
        if next.startswith("/platform-admin/") and not next.startswith("//")
        else "/platform-admin/organizations"
    )
    return RedirectResponse(
        url=safe_next,
        status_code=status.HTTP_303_SEE_OTHER,
    )


def platform_admin_google_redirect_uri() -> str:
    return (
        CONTROL_PLANE_PUBLIC_URL.rstrip("/")
        + "/platform-admin/google/callback"
    )


@app.get("/platform-admin/google/start")
async def platform_admin_google_start(
        request: Request,
        next: str = "/platform-admin/organizations"):
    identity, _user = await current_platform_admin_identity()
    safe_next = (
        next
        if next.startswith("/platform-admin/") and not next.startswith("//")
        else "/platform-admin/organizations"
    )
    try:
        authorization_url, flow = google_oidc_client.authorization_request(
            platform_admin_google_redirect_uri()
        )
    except PlatformAdminAuthError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse(
            url="/platform-admin/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    request.session["platform_admin_google_flow"] = {
        **flow,
        "identity_generation": identity.generation,
        "next": safe_next,
    }
    return RedirectResponse(
        url=authorization_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/platform-admin/gmail/start")
async def platform_admin_gmail_start(
        request: Request,
        _user=Depends(check_platform_admin)):
    identity, _authoritative_user = await current_platform_admin_identity()
    if not PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET:
        flash(request, "Gmail sender setup is not enabled.", "warning")
        return RedirectResponse(
            url="/platform-admin/account",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        authorization_url, flow = google_oidc_client.gmail_authorization_request(
            platform_admin_google_redirect_uri()
        )
    except PlatformAdminAuthError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse(
            url="/platform-admin/account",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    request.session["platform_admin_gmail_flow"] = {
        **flow,
        "identity_generation": identity.generation,
    }
    return RedirectResponse(
        url=authorization_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )


def store_gmail_refresh_token(refresh_token: str) -> None:
    if not PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET:
        raise PlatformAdminAuthError("Gmail sender setup is not enabled.")
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        client.add_secret_version(
            request={
                "parent": PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET,
                "payload": {"data": refresh_token.encode("utf-8")},
            }
        )
    except Exception as exc:
        raise PlatformAdminAuthError(
            "The Gmail credential could not be saved."
        ) from exc


@app.get("/platform-admin/google/callback")
async def platform_admin_google_callback(
        request: Request,
        code: str = "",
        state: str = "",
        error: str = ""):
    gmail_flow = request.session.pop("platform_admin_gmail_flow", None)
    if isinstance(gmail_flow, dict):
        try:
            user = await check_platform_admin(request)
            identity, authoritative_user = await current_platform_admin_identity()
            if user.id != authoritative_user.id or not secrets.compare_digest(
                identity.generation,
                str(gmail_flow.get("identity_generation", "")),
            ):
                raise PlatformAdminAuthError(
                    "The authorized administrator changed. Sign in again."
                )
            if (
                error
                or not code
                or not state
                or not secrets.compare_digest(
                    state, str(gmail_flow.get("state", ""))
                )
            ):
                raise PlatformAdminAuthError(
                    "Google Gmail authorization was canceled or could not be verified."
                )
            authorization = await asyncio.to_thread(
                google_oidc_client.exchange_gmail_code,
                code=code,
                redirect_uri=platform_admin_google_redirect_uri(),
                verifier=str(gmail_flow.get("verifier", "")),
                expected_nonce=str(gmail_flow.get("nonce", "")),
            )
            if not secrets.compare_digest(
                authorization.identity.email, identity.email
            ):
                raise PlatformAdminAuthError(
                    "Gmail must be authorized by the platform administrator account."
                )
            await asyncio.to_thread(
                store_gmail_refresh_token,
                authorization.refresh_token,
            )
        except (HTTPException, PlatformAdminAuthError) as exc:
            logging.warning("Gmail sender authorization failed: %s", exc)
            flash(request, str(exc), "warning")
            return RedirectResponse(
                url="/platform-admin/account",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        flash(
            request,
            "Gmail sender authorized. R2C Tracker is ready for its final mail deployment.",
            "success",
        )
        return RedirectResponse(
            url="/platform-admin/account",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    flow = request.session.pop("platform_admin_google_flow", None)
    if (
        error
        or not isinstance(flow, dict)
        or not code
        or not state
        or not secrets.compare_digest(state, str(flow.get("state", "")))
    ):
        flash(request, "Google sign-in was canceled or could not be verified.", "warning")
        return RedirectResponse(
            url="/platform-admin/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    identity, user = await current_platform_admin_identity()
    if not secrets.compare_digest(
        identity.generation,
        str(flow.get("identity_generation", "")),
    ):
        flash(request, "The authorized administrator changed. Sign in again.", "warning")
        return RedirectResponse(
            url="/platform-admin/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        google_identity = await asyncio.to_thread(
            google_oidc_client.exchange_code,
            code=code,
            redirect_uri=platform_admin_google_redirect_uri(),
            verifier=str(flow.get("verifier", "")),
            expected_nonce=str(flow.get("nonce", "")),
        )
    except PlatformAdminAuthError as exc:
        logging.warning("Google platform-admin login failed: %s", exc)
        flash(request, "Google sign-in could not be verified.", "warning")
        return RedirectResponse(
            url="/platform-admin/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not secrets.compare_digest(google_identity.email, identity.email):
        logging.warning(
            "Google platform-admin login rejected for non-authoritative email"
        )
        flash(request, "That Google account is not the authorized administrator.", "warning")
        return RedirectResponse(
            url="/platform-admin/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    clear_platform_admin_session(request)
    request.session["platform_admin_user_id"] = user.id
    request.session["platform_admin_identity_generation"] = identity.generation
    request.session["platform_admin_google_subject"] = google_identity.subject
    return RedirectResponse(
        url=str(flow.get("next", "/platform-admin/organizations")),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/platform-admin/setup/request")
async def platform_admin_setup_request(
        request: Request,
        email: Annotated[str, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "platform_admin_setup_request", form_token)
    identity, _user = await current_platform_admin_identity()
    if (
        platform_admin_email_sender.is_configured
        and secrets.compare_digest(email.strip().lower(), identity.email)
    ):
        token = await control_plane_store.issue_platform_admin_password_setup(
            email=identity.email,
            identity_generation=identity.generation,
        )
        if token:
            setup_url = (
                CONTROL_PLANE_PUBLIC_URL.rstrip("/")
                + "/platform-admin/setup#"
                + urlencode({"token": token})
            )
            try:
                await asyncio.to_thread(
                    platform_admin_email_sender.send_password_setup,
                    recipient=identity.email,
                    setup_url=setup_url,
                )
            except PlatformAdminAuthError:
                logging.exception("Unable to send platform-admin setup email")
    flash(
        request,
        "If that address is authorized, a five-minute setup link has been sent.",
        "info",
    )
    return RedirectResponse(
        url="/platform-admin/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/platform-admin/setup", response_class=HTMLResponse)
async def platform_admin_setup_page(request: Request):
    await current_platform_admin_identity()
    return platform_admin_setup_response(request)


def platform_admin_setup_response(
        request: Request,
        setup_token: str = "",
        status_code: int = status.HTTP_200_OK):
    return templates.TemplateResponse(
        request=request,
        name="platform_admin_setup.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "csrf_token": csrf_token(request, "platform_admin_password_setup"),
            "setup_token": setup_token,
        },
        status_code=status_code,
    )


@app.post("/platform-admin/setup")
async def platform_admin_setup_password(
        request: Request,
        setup_token: Annotated[str, Form()],
        new_password: Annotated[str, Form()],
        new_password_confirm: Annotated[str, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "platform_admin_password_setup", form_token)
    identity, _authoritative_user = await current_platform_admin_identity()
    if new_password != new_password_confirm:
        flash(request, "Passwords do not match.", "warning")
        return platform_admin_setup_response(
            request,
            setup_token,
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        user = await control_plane_store.set_platform_admin_password_from_token(
            token=setup_token,
            email=identity.email,
            identity_generation=identity.generation,
            new_password=new_password,
        )
    except ValueError as exc:
        flash(request, str(exc), "warning")
        return platform_admin_setup_response(
            request,
            setup_token,
            status.HTTP_400_BAD_REQUEST,
        )
    if user is None:
        flash(request, "That setup link is invalid, expired, or already used.", "warning")
        return RedirectResponse(
            url="/platform-admin/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    clear_platform_admin_session(request)
    request.session["platform_admin_user_id"] = user.id
    request.session["platform_admin_identity_generation"] = identity.generation
    flash(request, "Administrator password saved.", "success")
    return RedirectResponse(
        url="/platform-admin/organizations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/platform-admin/logout")
async def platform_admin_logout(
        request: Request,
        form_token: Annotated[str, Form()],
        _user=Depends(check_platform_admin)):
    verify_csrf(request, "platform_admin_account", form_token)
    clear_platform_admin_session(request)
    return RedirectResponse(
        url="/platform-admin/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/platform-admin/account", response_class=HTMLResponse)
async def platform_admin_account(
        request: Request,
        user=Depends(check_platform_admin)):
    return templates.TemplateResponse(
        request=request,
        name="platform_admin_account.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "platform_admin": user,
            "csrf_token": csrf_token(request, "platform_admin_password"),
            "gmail_setup_enabled": bool(
                PLATFORM_EMAIL_GMAIL_REFRESH_TOKEN_TARGET
                and google_oidc_client.is_configured
            ),
            "email_sender_configured": platform_admin_email_sender.is_configured,
        },
    )


@app.post("/platform-admin/account/password")
async def platform_admin_change_password(
        request: Request,
        current_password: Annotated[str, Form()],
        new_password: Annotated[str, Form()],
        new_password_confirm: Annotated[str, Form()],
        form_token: Annotated[str, Form()],
        user=Depends(check_platform_admin)):
    verify_csrf(request, "platform_admin_password", form_token)
    try:
        if new_password != new_password_confirm:
            raise ControlPlaneError("New passwords do not match.")
        await control_plane_store.change_platform_admin_password(
            user_id=user.id,
            current_password=current_password,
            new_password=new_password,
        )
        flash(request, "Platform administrator password changed.", "success")
    except (ControlPlaneError, ValueError) as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url="/platform-admin/account",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/platform-admin/organizations", response_class=HTMLResponse)
async def platform_admin_organizations(
        request: Request,
        user=Depends(check_platform_admin)):
    snapshot = await asyncio.to_thread(load_platform_billing_snapshot)
    audit_now = datetime.now(UTC)
    activation_url = request.session.pop("_platform_activation_url", None)
    provisioning_jobs = ()
    audit_events = ()
    audit_event_total = 0
    managed_access_requests = ()
    if control_plane_store is not None:
        (
            records,
            usage_aggregates,
            provisioning_jobs,
            audit_page,
            managed_access_requests,
            collected_mtd,
        ) = await asyncio.gather(
            control_plane_store.list_organizations(),
            control_plane_store.month_to_date_usage_aggregates(),
            control_plane_store.list_provisioning_jobs(),
            control_plane_store.search_audit_events(
                page_size=AUDIT_EVENT_RECENT_LIMIT,
                start_at=(
                    audit_now - timedelta(days=AUDIT_EVENT_RECENT_DAYS)
                ),
                categories=("administration", "billing", "enrollment"),
            ),
            control_plane_store.list_managed_access_requests(),
            control_plane_store.collected_month_to_date(),
        )
        audit_events = audit_page.events
        audit_event_total = audit_page.total
        await control_plane_store.record_audit_access(
            actor_id=user.id,
            details={"view": "recent_administrative_summary"},
            now=audit_now,
        )
        allocation_inputs = platform_allocation_inputs(records, usage_aggregates)
        allocated_costs, allocation_unallocated = allocate_platform_costs(
            snapshot.actual_cost_breakdown_mtd,
            allocation_inputs if snapshot.source_status == "ready" else {},
        )
        organizations = tuple(
            OrganizationBillingSummary(
                legal_name=record.legal_name,
                designator=record.designator,
                hostname=record.hostname,
                primary_admin_name=record.primary_admin_name,
                primary_admin_email=record.primary_admin_email,
                account_status=record.lifecycle_state,
                provisioning_status=record.provisioning_state,
                billing_mode=record.billing_mode,
                trial_ends_at=record.trial_ends_at,
                credit_balance=record.credit_balance,
                month_to_date_cost=allocated_costs.get(
                    record.id,
                    CostBreakdown(),
                ),
                primary_admin_postal_address=(
                    record.primary_admin_postal_address
                ),
                primary_admin_phone=record.primary_admin_phone,
                aggregate_usage=(
                    AggregateUsage(
                        requests=usage_aggregates[record.id].faa_proxy_requests,
                        network_bytes=usage_aggregates[record.id].network_bytes,
                        storage_byte_days=(
                            usage_aggregates[record.id].storage_byte_days
                        ),
                        compute_units=usage_aggregates[record.id].compute_units,
                        database_units=usage_aggregates[record.id].database_units,
                        turn_relay_bytes=usage_aggregates[record.id].turn_relay_bytes,
                    )
                    if record.id in usage_aggregates
                    else AggregateUsage()
                ),
            )
            for record in records
        )
        attributed = sum(
            (
                organization.month_to_date_cost.total
                for organization in organizations
            ),
            Decimal("0.00"),
        )
        snapshot = replace(
            snapshot,
            organizations=organizations,
            attributed_cost_mtd=attributed,
            unallocated_cost_mtd=allocation_unallocated,
            collected_mtd=collected_mtd,
            organizations_are_illustrative=False,
        )
    return templates.TemplateResponse(
        request=request,
        name="platform_admin.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "platform_snapshot": public_snapshot_dict(snapshot),
            "control_plane_enabled": (
                control_plane_store is not None
                and control_plane_tokens is not None
            ),
            "control_plane_simulation": CONTROL_PLANE_SIMULATION,
            "email_delivery_configured": (
                platform_admin_email_sender.is_configured
            ),
            "live_provisioning_ready": bool(
                organization_site_ready()
                and platform_admin_email_sender.is_configured
                and SESSION_COOKIE_HTTPS_ONLY
            ),
            "csrf_token": csrf_token(request, "platform_organizations"),
            "activation_url": activation_url,
            "provisioning_jobs": provisioning_jobs,
            "audit_events": audit_events,
            "audit_event_total": audit_event_total,
            "audit_recent_days": AUDIT_EVENT_RECENT_DAYS,
            "audit_recent_limit": AUDIT_EVENT_RECENT_LIMIT,
            "audit_retention_days": AUDIT_EVENT_RETENTION_DAYS,
            "managed_access_requests": managed_access_requests,
            "platform_admin": user,
            "account_csrf_token": csrf_token(
                request,
                "platform_admin_account",
            ),
        },
    )


AUDIT_ACTOR_TYPES = (
    "platform_admin",
    "organization_user",
    "organization_device",
    "device_enrollment",
    "billing_system",
)


def platform_audit_filters(
    *,
    start_date: str,
    end_date: str,
    organization: str,
    category: str,
    actor_type: str,
    event_type: str,
    now: Optional[datetime] = None,
) -> dict:
    reference = now or datetime.now(UTC)

    def parse_date(value: str, label: str, fallback: date) -> date:
        if not value:
            return fallback
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{label} must use YYYY-MM-DD.",
            ) from exc

    today = reference.date()
    parsed_start = parse_date(
        start_date,
        "Start date",
        today - timedelta(days=AUDIT_EVENT_HOT_DAYS),
    )
    parsed_end = parse_date(end_date, "End date", today)
    if parsed_start > parsed_end:
        raise HTTPException(
            status_code=400,
            detail="Start date must be on or before end date.",
        )
    if (parsed_end - parsed_start).days > AUDIT_EVENT_RETENTION_DAYS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Audit searches are limited to {AUDIT_EVENT_RETENTION_DAYS} "
                "days."
            ),
        )

    normalized_organization = organization.strip().upper()
    if normalized_organization and not re.fullmatch(
        r"[A-Z][A-Z0-9]{1,15}", normalized_organization
    ):
        raise HTTPException(status_code=400, detail="Invalid organization filter.")
    normalized_category = category.strip().lower()
    if (
        normalized_category
        and normalized_category not in AUDIT_EVENT_CATEGORY_PREFIXES
    ):
        raise HTTPException(status_code=400, detail="Invalid audit category.")
    normalized_actor = actor_type.strip().lower()
    if normalized_actor and normalized_actor not in AUDIT_ACTOR_TYPES:
        raise HTTPException(status_code=400, detail="Invalid actor type.")
    normalized_event = event_type.strip().lower()
    if normalized_event and not re.fullmatch(
        r"[a-z0-9][a-z0-9_.-]{0,79}", normalized_event
    ):
        raise HTTPException(status_code=400, detail="Invalid event type.")

    return {
        "start_date": parsed_start.isoformat(),
        "end_date": parsed_end.isoformat(),
        "start_at": datetime.combine(parsed_start, datetime.min.time(), UTC),
        "end_at": datetime.combine(
            parsed_end + timedelta(days=1), datetime.min.time(), UTC
        ),
        "organization": normalized_organization,
        "category": normalized_category,
        "actor_type": normalized_actor,
        "event_type": normalized_event,
    }


def platform_audit_url(path: str, filters: dict, *, page: Optional[int] = None) -> str:
    params = {
        key: filters[key]
        for key in (
            "start_date",
            "end_date",
            "organization",
            "category",
            "actor_type",
            "event_type",
        )
        if filters.get(key)
    }
    if page is not None:
        params["page"] = page
    return path + ("?" + urlencode(params) if params else "")


@app.get("/platform-admin/audit", response_class=HTMLResponse)
async def platform_admin_audit(
        request: Request,
        start_date: str = Query("", max_length=10),
        end_date: str = Query("", max_length=10),
        organization: str = Query("", max_length=16),
        category: str = Query("", max_length=32),
        actor_type: str = Query("", max_length=32),
        event_type: str = Query("", max_length=80),
        page: int = Query(1, ge=1),
        user=Depends(check_platform_admin)):
    filters = platform_audit_filters(
        start_date=start_date,
        end_date=end_date,
        organization=organization,
        category=category,
        actor_type=actor_type,
        event_type=event_type,
    )
    audit_page, organizations = await asyncio.gather(
        control_plane_store.search_audit_events(
            page=page,
            page_size=AUDIT_EVENT_PAGE_SIZE,
            start_at=filters["start_at"],
            end_at=filters["end_at"],
            organization_designator=filters["organization"],
            actor_type=filters["actor_type"],
            event_type=filters["event_type"],
            categories=(filters["category"],) if filters["category"] else (),
        ),
        control_plane_store.list_organizations(),
    )
    await control_plane_store.record_audit_access(
        actor_id=user.id,
        details={
            "view": "audit_log",
            "page": audit_page.page,
            "organization": filters["organization"],
            "category": filters["category"],
            "actor_type": filters["actor_type"],
            "event_type": filters["event_type"],
        },
    )
    current_url = platform_audit_url(
        "/platform-admin/audit", filters, page=audit_page.page
    )
    return templates.TemplateResponse(
        request=request,
        name="platform_admin_audit.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "platform_admin": user,
            "audit_page": audit_page,
            "audit_filters": filters,
            "audit_categories": tuple(AUDIT_EVENT_CATEGORY_PREFIXES),
            "audit_actor_types": AUDIT_ACTOR_TYPES,
            "organizations": organizations,
            "audit_retention_days": AUDIT_EVENT_RETENTION_DAYS,
            "audit_hot_days": AUDIT_EVENT_HOT_DAYS,
            "audit_csrf_token": csrf_token(request, "platform_admin_audit"),
            "current_url": current_url,
            "previous_url": (
                platform_audit_url(
                    "/platform-admin/audit", filters, page=audit_page.page - 1
                )
                if audit_page.page > 1
                else ""
            ),
            "next_url": (
                platform_audit_url(
                    "/platform-admin/audit", filters, page=audit_page.page + 1
                )
                if audit_page.page < audit_page.total_pages
                else ""
            ),
            "export_url": platform_audit_url(
                "/platform-admin/audit.csv", filters
            ),
        },
    )


@app.get("/platform-admin/audit.csv")
async def platform_admin_audit_export(
        start_date: str = Query("", max_length=10),
        end_date: str = Query("", max_length=10),
        organization: str = Query("", max_length=16),
        category: str = Query("", max_length=32),
        actor_type: str = Query("", max_length=32),
        event_type: str = Query("", max_length=80),
        user=Depends(check_platform_admin)):
    filters = platform_audit_filters(
        start_date=start_date,
        end_date=end_date,
        organization=organization,
        category=category,
        actor_type=actor_type,
        event_type=event_type,
    )
    audit_page = await control_plane_store.search_audit_events(
        page_size=AUDIT_EVENT_EXPORT_LIMIT,
        start_at=filters["start_at"],
        end_at=filters["end_at"],
        organization_designator=filters["organization"],
        actor_type=filters["actor_type"],
        event_type=filters["event_type"],
        categories=(filters["category"],) if filters["category"] else (),
    )
    if audit_page.total > AUDIT_EVENT_EXPORT_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(
                "The filtered export is too large. Narrow the date range or "
                "other filters."
            ),
        )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow((
        "created_at_utc",
        "organization",
        "event_type",
        "actor_type",
        "actor_id",
        "retention_hold",
    ))
    for event in audit_page.events:
        writer.writerow((
            event.created_at.isoformat(),
            event.designator or "Platform",
            event.event_type,
            event.actor_type,
            event.actor_id,
            "yes" if event.retention_hold else "no",
        ))
    await control_plane_store.record_audit_access(
        actor_id=user.id,
        event_type="audit.exported",
        details={
            "event_count": audit_page.total,
            "organization": filters["organization"],
            "category": filters["category"],
            "actor_type": filters["actor_type"],
            "event_type": filters["event_type"],
        },
    )
    filename = (
        "r2c_control_plane_audit_"
        f"{filters['start_date']}_{filters['end_date']}.csv"
    )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/platform-admin/audit/{event_id}/retention-hold")
async def platform_admin_audit_retention_hold(
        request: Request,
        event_id: str,
        action: Annotated[str, Form()],
        form_token: Annotated[str, Form()],
        return_to: Annotated[str, Form()] = "/platform-admin/audit",
        user=Depends(check_platform_admin)):
    verify_csrf(request, "platform_admin_audit", form_token)
    if action not in {"place", "release"}:
        raise HTTPException(status_code=400, detail="Invalid retention-hold action.")
    try:
        await control_plane_store.set_audit_event_retention_hold(
            event_id=event_id,
            retention_hold=action == "place",
            actor_id=user.id,
        )
        flash(
            request,
            "Retention hold placed." if action == "place" else "Retention hold released.",
            "success",
        )
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    safe_return = (
        return_to
        if return_to.startswith("/platform-admin/audit")
        and not return_to.startswith("//")
        else "/platform-admin/audit"
    )
    return RedirectResponse(url=safe_return, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/platform-admin/organizations")
async def platform_admin_create_organization(
        request: Request,
        legal_name: Annotated[str, Form()],
        designator: Annotated[str, Form()],
        admin_name: Annotated[str, Form()],
        admin_email: Annotated[str, Form()],
        postal_address: Annotated[str, Form()],
        form_token: Annotated[str, Form()],
        admin_phone: Annotated[str, Form()] = "",
        user=Depends(check_platform_admin)):
    verify_csrf(request, "platform_organizations", form_token)
    if control_plane_store is None or control_plane_tokens is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The control-plane database and signing key are required.",
        )
    if not CONTROL_PLANE_SIMULATION and not platform_admin_email_sender.is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Live organization provisioning requires the configured "
                "outbound email sender."
            ),
        )
    try:
        organization = await control_plane_store.create_organization(
            legal_name=legal_name,
            designator=designator,
            admin_name=admin_name,
            admin_email=admin_email,
            admin_phone=admin_phone,
            postal_address=postal_address,
            actor_id=user.id,
            simulation=CONTROL_PLANE_SIMULATION,
        )
        invitation = await control_plane_store.get_invitation(
            organization.designator,
            organization.primary_admin_email,
        )
        if invitation is None:
            raise ControlPlaneError(
                "Organization created, but its administrator invitation is unavailable."
            )
        if CONTROL_PLANE_SIMULATION:
            request.session["_platform_activation_url"] = (
                control_plane_tokens.activation_url(invitation)
            )
        else:
            activation_url = control_plane_tokens.activation_url(invitation)
            await asyncio.to_thread(
                platform_admin_email_sender.send_organization_activation,
                recipient=organization.primary_admin_email,
                administrator_name=organization.primary_admin_name,
                organization_name=organization.legal_name,
                designator=organization.designator,
                activation_url=activation_url,
            )
            await control_plane_store.mark_organization_invitation_sent(
                organization_id=organization.id,
                actor_id=user.id,
            )
        flash(
            request,
            (
                f"{organization.designator} created in "
                f"{'simulation' if CONTROL_PLANE_SIMULATION else 'live'} mode."
            ),
            "success",
        )
    except (
        ControlPlaneError,
        DuplicateOrganizationError,
        InvalidOrganizationError,
        PlatformAdminAuthError,
        ValueError,
    ) as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url="/platform-admin/organizations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/platform-admin/organizations/{designator}/archive")
async def platform_admin_archive_organization(
        request: Request,
        designator: str,
        confirmation: Annotated[str, Form()],
        form_token: Annotated[str, Form()],
        administrator_contact: Annotated[str, Form()] = "",
        contact_confirmed: Annotated[str, Form()] = "",
        user=Depends(check_platform_admin)):
    verify_csrf(request, "platform_organizations", form_token)
    try:
        clean_designator = designator.strip().upper()
        if not secrets.compare_digest(
            confirmation.strip().upper(),
            clean_designator,
        ):
            raise ControlPlaneError(
                f"Type {clean_designator} to confirm organization archival."
            )
        if contact_confirmed.strip().lower() not in {"1", "true", "yes", "on"}:
            raise ControlPlaneError(
                "Confirm direct contact with the organization administrator before archival."
            )
        organization = await control_plane_store.archive_organization(
            designator=clean_designator,
            actor_id=user.id,
            administrator_contact=administrator_contact,
        )
        clear_organization_session(request)
        flash(
            request,
            (
                f"{organization.designator} archived. Its site access, users, "
                "enrollment campaigns, device credentials, and active streams "
                "have been disabled; its designator remains reserved."
            ),
            "success",
        )
    except (ControlPlaneError, InvalidOrganizationError, ValueError) as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url="/platform-admin/organizations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/platform-admin/organizations/{designator}/contact")
async def platform_admin_update_organization_contact(
        request: Request,
        designator: str,
        legal_name: Annotated[str, Form()],
        admin_name: Annotated[str, Form()],
        admin_email: Annotated[str, Form()],
        admin_phone: Annotated[str, Form()],
        postal_address: Annotated[str, Form()],
        form_token: Annotated[str, Form()],
        user=Depends(check_platform_admin)):
    verify_csrf(request, "platform_organizations", form_token)
    if control_plane_store is None or control_plane_tokens is None:
        raise HTTPException(status_code=503, detail="Organization administration is not configured.")
    if not platform_admin_email_sender.is_configured:
        raise HTTPException(status_code=503, detail="Administrator email is not configured.")
    try:
        update_record = await control_plane_store.update_organization_administrator(
            designator=designator,
            legal_name=legal_name,
            admin_name=admin_name,
            admin_email=admin_email,
            admin_phone=admin_phone,
            postal_address=postal_address,
            actor_id=user.id,
        )
        organization = update_record.organization
        if update_record.administrator_changed:
            invitation = await control_plane_store.get_invitation(
                organization.designator,
                organization.primary_admin_email,
            )
            if invitation is None:
                raise ControlPlaneError("New administrator invitation is unavailable.")
            await asyncio.to_thread(
                platform_admin_email_sender.send_organization_activation,
                recipient=organization.primary_admin_email,
                administrator_name=organization.primary_admin_name,
                organization_name=organization.legal_name,
                designator=organization.designator,
                activation_url=control_plane_tokens.activation_url(invitation),
            )
            await asyncio.to_thread(
                platform_admin_email_sender.send_organization_administrator_changed,
                recipient=update_record.old_email,
                former_administrator_name=update_record.old_name,
                organization_name=organization.legal_name,
                designator=organization.designator,
                new_administrator_name=organization.primary_admin_name,
                new_administrator_email=organization.primary_admin_email,
            )
            flash(
                request,
                (
                    f"{organization.designator} administrator replaced. An activation "
                    f"invitation was sent to {organization.primary_admin_email}, and "
                    f"an accountability notice was sent to {update_record.old_email}."
                ),
                "success",
            )
        else:
            flash(request, f"{organization.designator} contact information updated.", "success")
    except (ControlPlaneError, InvalidOrganizationError, PlatformAdminAuthError, ValueError) as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url="/platform-admin/organizations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/platform-admin/organizations/{designator}/unarchive")
async def platform_admin_unarchive_organization(
        request: Request,
        designator: str,
        form_token: Annotated[str, Form()],
        user=Depends(check_platform_admin)):
    verify_csrf(request, "platform_organizations", form_token)
    if control_plane_store is None or control_plane_tokens is None:
        raise HTTPException(status_code=503, detail="Organization administration is not configured.")
    if not platform_admin_email_sender.is_configured:
        raise HTTPException(status_code=503, detail="Administrator email is not configured.")
    try:
        organization = await control_plane_store.unarchive_organization(
            designator=designator,
            actor_id=user.id,
        )
        invitation = await control_plane_store.get_invitation(
            organization.designator,
            organization.primary_admin_email,
        )
        if invitation is None:
            raise ControlPlaneError("Restored administrator invitation is unavailable.")
        await asyncio.to_thread(
            platform_admin_email_sender.send_organization_activation,
            recipient=organization.primary_admin_email,
            administrator_name=organization.primary_admin_name,
            organization_name=organization.legal_name,
            designator=organization.designator,
            activation_url=control_plane_tokens.activation_url(invitation),
        )
        flash(
            request,
            (
                f"{organization.designator} unarchived. A fresh administrator invitation "
                "was sent; prior device credentials and enrollment campaigns remain revoked."
            ),
            "success",
        )
    except (ControlPlaneError, InvalidOrganizationError, PlatformAdminAuthError, ValueError) as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url="/platform-admin/organizations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/platform-admin/organizations/{designator}/send-invitation")
async def platform_admin_send_organization_invitation(
        request: Request,
        designator: str,
        form_token: Annotated[str, Form()],
        user=Depends(check_platform_admin)):
    verify_csrf(request, "platform_organizations", form_token)
    if CONTROL_PLANE_SIMULATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email invitations are disabled in simulation mode.",
        )
    if (
        control_plane_store is None
        or control_plane_tokens is None
        or not platform_admin_email_sender.is_configured
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Live invitation email is not configured.",
        )
    try:
        organization = await control_plane_store.get_organization(designator)
        if organization is None:
            raise ControlPlaneError("Organization not found.")
        if organization.provisioning_state == "ready":
            await asyncio.to_thread(
                platform_admin_email_sender.send_organization_access,
                recipient=organization.primary_admin_email,
                administrator_name=organization.primary_admin_name,
                organization_name=organization.legal_name,
                designator=organization.designator,
                login_url=(
                    CONTROL_PLANE_PUBLIC_URL.rstrip("/")
                    + f"/{organization.designator.lower()}/login"
                ),
            )
            await control_plane_store.mark_organization_access_email_sent(
                organization_id=organization.id,
                actor_id=user.id,
            )
            flash(
                request,
                f"Administrator access email sent to {organization.primary_admin_email}.",
                "success",
            )
        else:
            invitation = await control_plane_store.renew_invitation(
                organization.designator,
                organization.primary_admin_email,
            )
            await asyncio.to_thread(
                platform_admin_email_sender.send_organization_activation,
                recipient=organization.primary_admin_email,
                administrator_name=organization.primary_admin_name,
                organization_name=organization.legal_name,
                designator=organization.designator,
                activation_url=control_plane_tokens.activation_url(invitation),
            )
            await control_plane_store.mark_organization_invitation_sent(
                organization_id=organization.id,
                actor_id=user.id,
            )
            flash(
                request,
                f"Activation invitation sent to {organization.primary_admin_email}.",
                "success",
            )
    except (ControlPlaneError, InvalidOrganizationError, PlatformAdminAuthError) as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url="/platform-admin/organizations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/{designator}/activate", response_class=HTMLResponse)
async def organization_activate_page(
        request: Request,
        designator: str,
        token: str):
    if not organization_site_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Organization administration is not configured.",
        )
    identity_name = await organization_page_identity(request, designator)
    try:
        claims = control_plane_tokens.decode_activation(token)
        if claims.designator.lower() != designator.lower():
            raise EnrollmentTokenError("Administrator activation link is invalid.")
        invitation = await control_plane_store.get_invitation(
            claims.designator,
            claims.email,
        )
        if (
            invitation is None
            or invitation.user_id != claims.user_id
            or invitation.organization_id != claims.organization_id
            or not secrets.compare_digest(
                invitation.activation_nonce,
                claims.nonce,
            )
        ):
            raise EnrollmentTokenError(
                "Administrator activation link is invalid or already used."
            )
    except (ControlPlaneError, EnrollmentTokenError, InvalidOrganizationError) as exc:
        return templates.TemplateResponse(
            request=request,
            name="organization_activate.html",
            context={
                "request": request,
                "enable_live_refresh": False,
                "include_leaflet": False,
                "include_datetime_script": False,
                "activation_error": str(exc),
                "organization_page_designator": designator.upper(),
                "organization_identity_name": identity_name,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return templates.TemplateResponse(
        request=request,
        name="organization_activate.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "activation_error": None,
            "designator": claims.designator,
            "email": claims.email,
            "token": token,
            "csrf_token": csrf_token(request, "organization_activation"),
            "google_login_enabled": google_oidc_client.is_configured,
            "microsoft_login_enabled": microsoft_oidc_client.is_configured,
            "organization_page_designator": claims.designator,
            "organization_identity_name": identity_name,
        },
    )


@app.post("/{designator}/activate")
async def organization_activate_with_password(
        request: Request,
        designator: str,
        token: Annotated[str, Form()],
        password: Annotated[str, Form()],
        password_confirm: Annotated[str, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_activation", form_token)
    if password != password_confirm:
        flash(request, "Passwords do not match.", "warning")
        return RedirectResponse(
            url=f"/{designator.lower()}/activate?{urlencode({'token': token})}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        claims = control_plane_tokens.decode_activation(token)
        if claims.designator.lower() != designator.lower():
            raise ControlPlaneError("The activation link is invalid.")
        user = await control_plane_store.activate_owner(
            claims.designator,
            claims.email,
            password,
            activation_nonce=claims.nonce,
        )
    except (
        ControlPlaneError,
        EnrollmentTokenError,
        InvalidOrganizationError,
        ValueError,
    ) as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse(
            url=f"/{designator.lower()}/activate?{urlencode({'token': token})}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    clear_organization_session(request)
    request.session["organization_user_id"] = user.id
    request.session["organization_designator"] = claims.designator
    flash(request, "Organization account activated.", "success")
    organization = await control_plane_store.get_organization(claims.designator)
    return RedirectResponse(
        url=organization_landing_path(organization, user),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/activate/google")
async def organization_activate_with_google(
        request: Request,
        designator: str,
        token: Annotated[str, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_activation", form_token)
    if not organization_site_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Organization administration is not configured.",
        )
    try:
        claims = control_plane_tokens.decode_activation(token)
        if claims.designator.lower() != designator.lower():
            raise ControlPlaneError("The activation link is invalid.")
        invitation = await control_plane_store.get_invitation(
            claims.designator,
            claims.email,
        )
        if (
            invitation is None
            or invitation.user_id != claims.user_id
            or invitation.organization_id != claims.organization_id
            or not secrets.compare_digest(invitation.activation_nonce, claims.nonce)
        ):
            raise ControlPlaneError(
                "The administrator invitation is invalid or already used."
            )
        authorization_url, flow = google_oidc_client.authorization_request(
            organization_google_redirect_uri()
        )
    except (
        ControlPlaneError,
        EnrollmentTokenError,
        InvalidOrganizationError,
        PlatformAdminAuthError,
        ValueError,
    ) as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse(
            url=(
                f"/{designator.lower()}/activate?"
                f"{urlencode({'token': token})}"
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    request.session["organization_google_flow"] = {
        **flow,
        "organization_id": claims.organization_id,
        "designator": claims.designator,
        "next": "",
        "activation_email": claims.email,
        "activation_nonce": claims.nonce,
    }
    return RedirectResponse(
        url=authorization_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/activate/microsoft")
async def organization_activate_with_microsoft(
        request: Request,
        designator: str,
        token: Annotated[str, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_activation", form_token)
    if not organization_site_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Organization administration is not configured.",
        )
    try:
        claims = control_plane_tokens.decode_activation(token)
        if claims.designator.lower() != designator.lower():
            raise ControlPlaneError("The activation link is invalid.")
        invitation = await control_plane_store.get_invitation(
            claims.designator, claims.email
        )
        if (
            invitation is None
            or invitation.user_id != claims.user_id
            or invitation.organization_id != claims.organization_id
            or not secrets.compare_digest(invitation.activation_nonce, claims.nonce)
        ):
            raise ControlPlaneError(
                "The administrator invitation is invalid or already used."
            )
        authorization_url, flow = microsoft_oidc_client.authorization_request(
            organization_microsoft_redirect_uri()
        )
    except (
        ControlPlaneError,
        EnrollmentTokenError,
        InvalidOrganizationError,
        PlatformAdminAuthError,
        ValueError,
    ) as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse(
            url=(
                f"/{designator.lower()}/activate?"
                f"{urlencode({'token': token})}"
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    request.session["organization_microsoft_flow"] = {
        **flow,
        "organization_id": claims.organization_id,
        "designator": claims.designator,
        "activation_email": claims.email,
        "activation_nonce": claims.nonce,
    }
    return RedirectResponse(
        url=authorization_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )


def organization_safe_login_next(designator: str, requested: str) -> str:
    organization_path = f"/{designator.lower()}"
    if requested == organization_path:
        return requested
    if re.fullmatch(
        re.escape(organization_path)
        + r"/device-reauthenticate\?token=[A-Za-z0-9._~-]{20,4096}",
        requested,
    ):
        return requested
    if re.fullmatch(
        re.escape(organization_path)
        + r"/streams/(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2}){1,480}"
        + r"(?:/(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2}){1,480})?",
        requested,
    ):
        return requested
    return ""


@app.get("/{designator}/login", response_class=HTMLResponse)
async def organization_login_page(
        request: Request,
        designator: str,
        next: str = ""):
    if not organization_site_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Organization administration is not configured.",
        )
    organization = await control_plane_store.get_organization(designator)
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    identity_name = await organization_page_identity(
        request,
        organization.designator,
    )
    next_path = organization_safe_login_next(organization.designator, next)
    return templates.TemplateResponse(
        request=request,
        name="organization_login.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "organization": organization,
            "csrf_token": csrf_token(request, "organization_login"),
            "google_login_enabled": google_oidc_client.is_configured,
            "microsoft_login_enabled": microsoft_oidc_client.is_configured,
            "password_login_enabled": True,
            "google_start_url": (
                f"/{organization.designator.lower()}/google/start"
                + ("?" + urlencode({"next": next_path}) if next_path else "")
            ),
            "microsoft_start_url": (
                f"/{organization.designator.lower()}/microsoft/start"
                + ("?" + urlencode({"next": next_path}) if next_path else "")
            ),
            "next_path": next_path,
            "organization_page_designator": organization.designator,
            "organization_identity_name": identity_name,
        },
    )


@app.post("/{designator}/login")
async def organization_login(
        request: Request,
        designator: str,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        form_token: Annotated[str, Form()],
        next: Annotated[str, Form()] = ""):
    """Authenticate an organization user with an R2C password."""
    verify_csrf(request, "organization_login", form_token)
    if not organization_site_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Organization administration is not configured.",
        )
    try:
        user = await control_plane_store.authenticate_user(
            designator,
            email,
            password,
        )
    except InvalidOrganizationError:
        user = None
    if user is None:
        flash(request, "Incorrect email or password.", "warning")
        login_path = f"/{designator.lower()}/login"
        return RedirectResponse(
            url=(
                login_path + "?" + urlencode({"next": next})
                if organization_safe_login_next(designator, next)
                else login_path
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    organization = await control_plane_store.get_organization(designator)
    clear_organization_session(request)
    request.session["organization_user_id"] = user.id
    request.session["organization_designator"] = organization.designator
    next_path = organization_safe_login_next(organization.designator, next)
    return RedirectResponse(
        url=next_path or organization_landing_path(organization, user),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/{designator}/forgot-password", response_class=HTMLResponse)
async def organization_forgot_password_page(
        request: Request,
        designator: str):
    organization = await control_plane_store.get_organization(designator)
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return templates.TemplateResponse(
        request=request,
        name="organization_forgot_password.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "organization": organization,
            "csrf_token": csrf_token(request, "organization_password_reset_request"),
            "organization_page_designator": organization.designator,
            "organization_identity_name": await organization_page_identity(
                request, organization.designator
            ),
        },
    )


@app.post("/{designator}/forgot-password")
async def organization_forgot_password_request(
        request: Request,
        designator: str,
        email: Annotated[str, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_password_reset_request", form_token)
    organization = await control_plane_store.get_organization(designator)
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    if platform_admin_email_sender.is_configured:
        try:
            token = await control_plane_store.issue_organization_password_reset(
                designator=organization.designator,
                email=email,
            )
        except InvalidOrganizationError:
            token = None
        if token:
            reset_url = (
                CONTROL_PLANE_PUBLIC_URL.rstrip("/")
                + f"/{organization.designator.lower()}/reset-password#"
                + urlencode({"token": token})
            )
            try:
                await asyncio.to_thread(
                    platform_admin_email_sender.send_organization_password_reset,
                    recipient=email.strip().lower(),
                    organization_name=organization.legal_name,
                    designator=organization.designator,
                    reset_url=reset_url,
                )
            except PlatformAdminAuthError:
                logging.exception("Unable to send organization password reset email")
    flash(
        request,
        "If that address is registered, a password-reset link has been sent.",
        "info",
    )
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def organization_reset_password_response(
        request: Request,
        designator: str,
        reset_token: str = "",
        status_code: int = status.HTTP_200_OK):
    return templates.TemplateResponse(
        request=request,
        name="organization_reset_password.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "designator": designator.upper(),
            "reset_token": reset_token,
            "csrf_token": csrf_token(request, "organization_password_reset"),
            "organization_page_designator": designator.upper(),
            "organization_identity_name": "Guest",
        },
        status_code=status_code,
    )


@app.get("/{designator}/reset-password", response_class=HTMLResponse)
async def organization_reset_password_page(request: Request, designator: str):
    organization = await control_plane_store.get_organization(designator)
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return organization_reset_password_response(request, organization.designator)


@app.post("/{designator}/reset-password")
async def organization_reset_password(
        request: Request,
        designator: str,
        reset_token: Annotated[str, Form()],
        new_password: Annotated[str, Form()],
        new_password_confirm: Annotated[str, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_password_reset", form_token)
    if new_password != new_password_confirm:
        flash(request, "Passwords do not match.", "warning")
        return organization_reset_password_response(
            request, designator, reset_token, status.HTTP_400_BAD_REQUEST
        )
    try:
        user = await control_plane_store.set_organization_password_from_reset(
            designator=designator,
            token=reset_token,
            new_password=new_password,
        )
    except ValueError as exc:
        flash(request, str(exc), "warning")
        return organization_reset_password_response(
            request, designator, reset_token, status.HTTP_400_BAD_REQUEST
        )
    if user is None:
        flash(request, "That reset link is invalid, expired, or already used.", "warning")
        return RedirectResponse(
            url=f"/{designator.lower()}/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    clear_organization_session(request)
    request.session["organization_user_id"] = user.id
    request.session["organization_designator"] = designator.upper()
    flash(request, "Password reset complete.", "success")
    return RedirectResponse(
        url=f"/{designator.lower()}/admin",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def organization_google_redirect_uri() -> str:
    return (
        CONTROL_PLANE_PUBLIC_URL.rstrip("/")
        + "/google/callback"
    )


def organization_microsoft_redirect_uri() -> str:
    return CONTROL_PLANE_PUBLIC_URL.rstrip("/") + "/microsoft/callback"


@app.get("/{designator}/microsoft/start")
async def organization_microsoft_start(
        request: Request,
        designator: str,
        next: str = ""):
    if not organization_site_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Organization administration is not configured.",
        )
    try:
        organization = await control_plane_store.get_organization(designator)
    except InvalidOrganizationError:
        organization = None
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        authorization_url, flow = microsoft_oidc_client.authorization_request(
            organization_microsoft_redirect_uri()
        )
    except PlatformAdminAuthError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse(
            url=f"/{organization.designator.lower()}/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    request.session["organization_microsoft_flow"] = {
        **flow,
        "organization_id": organization.id,
        "designator": organization.designator,
        "next": organization_safe_login_next(organization.designator, next),
    }
    return RedirectResponse(
        url=authorization_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/{designator}/google/start")
async def organization_google_start(
        request: Request,
        designator: str,
        next: str = ""):
    if not organization_site_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Organization administration is not configured.",
        )
    try:
        organization = await control_plane_store.get_organization(designator)
    except InvalidOrganizationError:
        organization = None
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        authorization_url, flow = google_oidc_client.authorization_request(
            organization_google_redirect_uri()
        )
    except PlatformAdminAuthError as exc:
        flash(request, str(exc), "warning")
        return RedirectResponse(
            url=f"/{organization.designator.lower()}/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    request.session["organization_google_flow"] = {
        **flow,
        "organization_id": organization.id,
        "designator": organization.designator,
        "next": organization_safe_login_next(organization.designator, next),
    }
    return RedirectResponse(
        url=authorization_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/google/callback")
async def organization_google_callback(
        request: Request,
        code: str = "",
        state: str = "",
        error: str = ""):
    flow = request.session.pop("organization_google_flow", None)
    fallback_designator = (
        str(flow.get("designator", "")).lower()
        if isinstance(flow, dict)
        else ""
    )
    fallback_url = (
        f"/{fallback_designator}/login"
        if fallback_designator
        else "/"
    )
    if (
        error
        or not isinstance(flow, dict)
        or not code
        or not state
        or not secrets.compare_digest(state, str(flow.get("state", "")))
    ):
        flash(request, "Google sign-in was canceled or could not be verified.", "warning")
        return RedirectResponse(
            url=fallback_url,
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        organization = await control_plane_store.get_organization(
            str(flow.get("designator", ""))
        )
    except InvalidOrganizationError:
        organization = None
    if (
        organization is None
        or not secrets.compare_digest(
            organization.id,
            str(flow.get("organization_id", "")),
        )
    ):
        flash(request, "The organization changed. Sign in again.", "warning")
        return RedirectResponse(
            url=fallback_url,
            status_code=status.HTTP_303_SEE_OTHER,
        )
    google_identity = None
    try:
        google_identity = await asyncio.to_thread(
            google_oidc_client.exchange_code,
            code=code,
            redirect_uri=organization_google_redirect_uri(),
            verifier=str(flow.get("verifier", "")),
            expected_nonce=str(flow.get("nonce", "")),
        )
        user = await control_plane_store.authorize_google_user(
            organization.designator,
            google_identity.email,
            activation_nonce=(
                str(flow.get("activation_nonce", ""))
                if secrets.compare_digest(
                    google_identity.email,
                    str(flow.get("activation_email", "")),
                )
                else None
            ),
        )
    except (PlatformAdminAuthError, InvalidOrganizationError) as exc:
        logging.warning("Google organization login failed: %s", exc)
        user = None
    next_path = organization_safe_login_next(
        organization.designator,
        str(flow.get("next", "")),
    )
    if user is None:
        logging.warning(
            "Google organization login rejected for unauthorized email"
        )
        flash(
            request,
            "That Google account is not an active member of this organization.",
            "warning",
        )
        if "/device-reauthenticate?token=" in next_path:
            request.session["device_reauthentication_failures"] = min(
                99,
                int(request.session.get("device_reauthentication_failures", 0)) + 1,
            )
            request.session["device_reauthentication_last_email"] = (
                google_identity.email if google_identity is not None else ""
            )
            return RedirectResponse(
                url=next_path,
                status_code=status.HTTP_303_SEE_OTHER,
            )
        return RedirectResponse(
            url=f"/{organization.designator.lower()}/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    clear_organization_session(request)
    request.session["organization_user_id"] = user.id
    request.session["organization_designator"] = organization.designator
    request.session["organization_google_subject"] = google_identity.subject
    if flow.get("activation_nonce"):
        flash(request, "Organization account activated.", "success")
    return RedirectResponse(
        url=next_path or organization_landing_path(organization, user),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/{designator}/device-reauthenticate", response_class=HTMLResponse)
async def organization_device_reauthenticate(
        request: Request,
        designator: str,
        token: str = ""):
    if not organization_site_ready():
        raise HTTPException(status_code=503, detail="Organization login unavailable.")
    try:
        claims = control_plane_tokens.decode_device_reauthentication(token)
        organization = await control_plane_store.get_organization(designator)
    except (EnrollmentTokenError, InvalidOrganizationError):
        organization = None
        claims = None
    if (
        organization is None
        or claims is None
        or organization.id != claims.organization_id
        or organization.designator != claims.designator
    ):
        raise HTTPException(
            status_code=400,
            detail="Device reauthentication request is invalid or expired.",
        )
    credential = await control_plane_store.get_device_reauthentication_record(
        credential_id=claims.credential_id,
        organization_id=claims.organization_id,
    )
    if (
        credential is None
        or credential.reauth_requested_at is None
        or credential.reauth_requested_at.isoformat() != claims.requested_at
    ):
        raise HTTPException(
            status_code=409,
            detail="This device is no longer awaiting reauthentication.",
        )
    user_id = request.session.get("organization_user_id")
    session_designator = request.session.get("organization_designator")
    user = (
        await control_plane_store.get_user(user_id)
        if isinstance(user_id, str)
        and session_designator == organization.designator
        else None
    )
    completed = False
    authorized_email = ""
    if (
        user is not None
        and user.state == "active"
        and user.organization_id == organization.id
        and "r2c_device" in user.roles
    ):
        await control_plane_store.complete_device_reauthentication(
            credential_id=credential.id,
            organization_id=organization.id,
            user_id=user.id,
        )
        completed = True
        authorized_email = user.email
        request.session.pop("device_reauthentication_failures", None)
        request.session.pop("device_reauthentication_last_email", None)
        request.session.pop("device_reauthentication_last_user_id", None)
    elif user is not None and user.state == "active":
        if request.session.get("device_reauthentication_last_user_id") != user.id:
            request.session["device_reauthentication_failures"] = min(
                99,
                int(request.session.get("device_reauthentication_failures", 0)) + 1,
            )
            request.session["device_reauthentication_last_user_id"] = user.id
            request.session["device_reauthentication_last_email"] = user.email
        clear_organization_session(request)
    attempts = int(request.session.get("device_reauthentication_failures", 0))
    next_path = (
        f"/{organization.designator.lower()}/device-reauthenticate?"
        + urlencode({"token": token})
    )
    google_start_url = (
        f"/{organization.designator.lower()}/google/start?"
        + urlencode({"next": next_path})
    )
    return templates.TemplateResponse(
        request=request,
        name="device_reauthenticate.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "organization": organization,
            "organization_page_designator": organization.designator,
            "organization_identity_name": user.display_name if user else "Guest",
            "device_name": credential.device_name,
            "completed": completed,
            "authorized_email": authorized_email,
            "attempts": attempts,
            "last_email": request.session.get(
                "device_reauthentication_last_email", ""
            ),
            "google_start_url": google_start_url,
        },
    )


@app.get("/microsoft/callback")
async def organization_microsoft_callback(
        request: Request,
        code: str = "",
        state: str = "",
        error: str = ""):
    flow = request.session.pop("organization_microsoft_flow", None)
    fallback_designator = (
        str(flow.get("designator", "")).lower()
        if isinstance(flow, dict)
        else ""
    )
    fallback_url = f"/{fallback_designator}/login" if fallback_designator else "/"
    if (
        error
        or not isinstance(flow, dict)
        or not code
        or not state
        or not secrets.compare_digest(state, str(flow.get("state", "")))
    ):
        flash(
            request,
            "Microsoft sign-in was canceled or could not be verified.",
            "warning",
        )
        return RedirectResponse(url=fallback_url, status_code=status.HTTP_303_SEE_OTHER)
    try:
        organization = await control_plane_store.get_organization(
            str(flow.get("designator", ""))
        )
    except InvalidOrganizationError:
        organization = None
    if (
        organization is None
        or not secrets.compare_digest(
            organization.id, str(flow.get("organization_id", ""))
        )
    ):
        flash(request, "The organization changed. Sign in again.", "warning")
        return RedirectResponse(url=fallback_url, status_code=status.HTTP_303_SEE_OTHER)
    try:
        identity = await asyncio.to_thread(
            microsoft_oidc_client.exchange_code,
            code=code,
            redirect_uri=organization_microsoft_redirect_uri(),
            verifier=str(flow.get("verifier", "")),
            expected_nonce=str(flow.get("nonce", "")),
        )
        activation_nonce = (
            str(flow.get("activation_nonce", ""))
            if secrets.compare_digest(
                identity.email, str(flow.get("activation_email", ""))
            )
            else None
        )
        user = await control_plane_store.authorize_microsoft_user(
            organization.designator,
            identity.email,
            issuer=identity.issuer,
            subject=identity.subject,
            activation_nonce=activation_nonce,
        )
    except (PlatformAdminAuthError, InvalidOrganizationError) as exc:
        logging.warning("Microsoft organization login failed: %s", exc)
        user = None
    if user is None:
        logging.warning("Microsoft organization login rejected for unbound identity")
        flash(
            request,
            (
                "That Microsoft account is not linked to an active member of "
                "this organization. Use the current invitation link to connect it."
            ),
            "warning",
        )
        return RedirectResponse(
            url=f"/{organization.designator.lower()}/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    clear_organization_session(request)
    request.session["organization_user_id"] = user.id
    request.session["organization_designator"] = organization.designator
    request.session["organization_external_identity"] = {
        "provider": "microsoft",
        "issuer": identity.issuer,
        "subject": identity.subject,
    }
    if flow.get("activation_nonce"):
        flash(request, "Organization account activated.", "success")
    next_path = organization_safe_login_next(
        organization.designator, str(flow.get("next", ""))
    )
    return RedirectResponse(
        url=next_path or organization_landing_path(organization, user),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/logout")
async def organization_logout(
        request: Request,
        designator: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    clear_organization_session(request)
    return RedirectResponse(
        url=f"/{designator.lower()}/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/{designator}/admin", response_class=HTMLResponse)
async def organization_admin(request: Request, designator: str):
    organization, user = await require_organization_user(
        request,
        designator,
        required_roles=(
            "organization_owner",
            "billing_admin",
            "config_admin",
            "user_admin",
            "records_admin",
        ),
        redirect_to_login=True,
    )
    invitation_url = request.session.pop("_organization_invitation_url", None)
    users, campaigns, ledger_entries = await asyncio.gather(
        control_plane_store.list_users(organization.id),
        control_plane_store.list_enrollment_campaigns(organization.id),
        control_plane_store.list_ledger(organization.id),
    )
    can_manage_device_credentials = bool(
        {"organization_owner", "user_admin"}.intersection(user.roles)
    )
    device_credentials = (
        await control_plane_store.list_device_credentials(organization.id)
        if can_manage_device_credentials
        else ()
    )
    credential_now = datetime.now(UTC)
    usable_device_credentials = tuple(
        credential for credential in device_credentials
        if credential.state == "active" and credential.expires_at >= credential_now
    )
    renewable_device_credentials = tuple(
        credential for credential in device_credentials
        if credential.state not in {"revoked", "reauth_required"}
    )
    expiring_device_credentials = tuple(
        credential for credential in usable_device_credentials
        if credential.expires_at <= credential_now + timedelta(days=30)
    )
    connected_config_sources = ()
    config_proposal = None
    config_releases = ()
    current_config_version_ms = 0
    config_proposal_wait_error = ""
    if {"organization_owner", "config_admin"}.intersection(user.roles):
        (
            connected_config_sources,
            config_proposal,
            config_releases,
            current_config_version_ms,
        ) = await asyncio.gather(
            r2c_hub.list_connected_config_sources(organization.id),
            control_plane_store.get_organization_config_proposal(organization.id),
            control_plane_store.list_organization_config_releases(organization.id),
            control_plane_store.get_organization_config_version_ms(organization.id),
        )
        if config_proposal and config_proposal.state == "awaiting_device":
            proposal_source = next(
                (
                    source for source in connected_config_sources
                    if source.id == config_proposal.source_device_credential_id
                ),
                None,
            )
            if proposal_source is None:
                config_proposal_wait_error = (
                    f"{config_proposal.source_device_name} disconnected before "
                    "returning its configuration. Discard this request and try again."
                )
            elif not proposal_source.supports_organization_config:
                config_proposal_wait_error = organization_config_upgrade_message(
                    proposal_source
                )
    organization_cost = None
    billing_snapshot = None
    beta_allowance = None
    if {"organization_owner", "billing_admin"}.intersection(user.roles):
        billing_snapshot, records, usage_aggregates, beta_allowance = await asyncio.gather(
            asyncio.to_thread(load_platform_billing_snapshot),
            control_plane_store.list_organizations(),
            control_plane_store.month_to_date_usage_aggregates(),
            control_plane_store.get_extended_beta_allowance(organization.id),
        )
        allocation_inputs = platform_allocation_inputs(records, usage_aggregates)
        allocated_costs, _unallocated = allocate_platform_costs(
            billing_snapshot.actual_cost_breakdown_mtd,
            allocation_inputs if billing_snapshot.source_status == "ready" else {},
        )
        organization_cost = allocated_costs.get(organization.id)
    return templates.TemplateResponse(
        request=request,
        name="organization_admin.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "organization": organization,
            "organization_user": user,
            "organization_users": users,
            "enrollment_campaigns": campaigns,
            "device_credentials": device_credentials,
            "usable_device_credentials": usable_device_credentials,
            "renewable_device_credentials": renewable_device_credentials,
            "expiring_device_credentials": expiring_device_credentials,
            "can_manage_device_credentials": can_manage_device_credentials,
            "ledger_entries": ledger_entries,
            "organization_cost": organization_cost,
            "beta_allowance": beta_allowance,
            "billing_snapshot": billing_snapshot,
            "csrf_token": csrf_token(request, "organization_admin"),
            "simulation": CONTROL_PLANE_SIMULATION,
            "invitation_url": invitation_url,
            "invitation_email_enabled": (
                platform_admin_email_sender.is_configured
            ),
            "organization_page_designator": organization.designator,
            "organization_identity_name": user.display_name,
            "role_descriptions": ROLE_DESCRIPTIONS,
            "connected_config_sources": connected_config_sources,
            "config_proposal": config_proposal,
            "config_releases": config_releases,
            "current_config_version_ms": current_config_version_ms,
            "config_proposal_wait_error": config_proposal_wait_error,
            "organization_config_min_app_build": (
                R2C_ORGANIZATION_CONFIG_MIN_APP_BUILD
            ),
            "has_compatible_config_sources": any(
                source.supports_organization_config
                for source in connected_config_sources
            ),
        },
    )


@app.post("/{designator}/admin/organization-config/request")
async def request_organization_config(
        request: Request,
        designator: str,
        device_credential_id: Annotated[str, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request, designator,
        required_roles=("organization_owner", "config_admin"),
        redirect_to_login=True,
    )
    connected = await r2c_hub.list_connected_config_sources(organization.id)
    selected = next(
        (item for item in connected if item.id == device_credential_id), None
    )
    if selected is None:
        flash(request, "That RID2Caltopo device is no longer connected.", "warning")
        return RedirectResponse(f"/{organization.designator.lower()}/admin#organization-config", status_code=303)
    if not selected.supports_organization_config:
        flash(request, organization_config_upgrade_message(selected), "warning")
        return RedirectResponse(
            f"/{organization.designator.lower()}/admin#organization-config",
            status_code=303,
        )
    try:
        proposal = await control_plane_store.start_organization_config_proposal(
            organization_id=organization.id,
            device_credential_id=selected.id,
            requested_by_user_id=user.id,
            source_device_name=selected.device_name,
        )
        delivered = await r2c_hub.send_organization_config_snapshot_request(
            device_credential_id=selected.id,
            request_id=proposal.id,
        )
        if not delivered:
            await control_plane_store.reject_organization_config_proposal(
                organization_id=organization.id, actor_user_id=user.id,
            )
            raise ControlPlaneError("The device disconnected before it could respond.")
        flash(request, f"Requested configuration from {selected.device_name}.", "success")
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(f"/{organization.designator.lower()}/admin#organization-config", status_code=303)


@app.post("/{designator}/admin/organization-config/approve")
async def approve_organization_config(
        request: Request,
        designator: str,
        comment: Annotated[str, Form()] = "",
        form_token: Annotated[str, Form()] = ""):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request, designator,
        required_roles=("organization_owner", "config_admin"),
        redirect_to_login=True,
    )
    try:
        release = await control_plane_store.approve_organization_config_proposal(
            organization_id=organization.id,
            actor_user_id=user.id,
            comment=comment,
        )
        flash(request, f"Published organization configuration {release.version_ms}.", "success")
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(f"/{organization.designator.lower()}/admin#organization-config", status_code=303)


@app.post("/{designator}/admin/organization-config/reject")
async def reject_organization_config(
        request: Request,
        designator: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request, designator,
        required_roles=("organization_owner", "config_admin"),
        redirect_to_login=True,
    )
    try:
        await control_plane_store.reject_organization_config_proposal(
            organization_id=organization.id, actor_user_id=user.id,
        )
        flash(request, "Discarded the proposed organization configuration.", "success")
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(f"/{organization.designator.lower()}/admin#organization-config", status_code=303)


@app.post("/{designator}/admin/organization-config/restore")
async def restore_organization_config(
        request: Request,
        designator: str,
        version_ms: Annotated[int, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request, designator,
        required_roles=("organization_owner", "config_admin"),
        redirect_to_login=True,
    )
    try:
        await control_plane_store.restore_organization_config_release(
            organization_id=organization.id,
            version_ms=version_ms,
            actor_user_id=user.id,
        )
        flash(request, f"Restored organization configuration {version_ms}.", "success")
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(f"/{organization.designator.lower()}/admin#organization-config", status_code=303)


async def require_organization_records_admin(
        request: Request,
        designator: str):
    return await require_organization_user(
        request,
        designator,
        required_roles=("organization_owner", "records_admin"),
        redirect_to_login=True,
    )


def organization_admin_csv_response(
        flights,
        designator: str) -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Flight", "Sar Id", "Remote Id", "UAS", "Incident", "Op Period",
        "Map Id", "Start Time", "End Time", "Start Lattitude",
        "Start Longitude", "Hours", "Distance (mi)", "Temp (F)",
        "Rel Humidity (%)", "Dew Pt (F)", "Precip (in)", "Wind (mph)",
        "Gusts (mph)", "Cloud Cover (%)", "Time Of Day", "Archive Path",
    ])
    for flight in flights:
        writer.writerow([
            flight.id,
            flight.sar_id.upper(),
            (flight.remote_id or "").upper(),
            flight.uas.lower(),
            flight.incident,
            flight.op_period,
            flight.map_id.upper(),
            format_datetime(flight.start_time.replace(tzinfo=UTC)),
            format_datetime(flight.end_time.replace(tzinfo=UTC)),
            flight.start_lat,
            flight.start_lng,
            flight.hours,
            flight.distance_mi,
            flight.temp_f,
            flight.rhum_pct,
            flight.dewpt_f,
            flight.precip_in,
            flight.wind_mph,
            flight.gusts_mph,
            flight.cloudcvr_pct,
            flight.timeofday,
            flight.archive_relpath or "",
        ])
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"r2c_{designator.lower()}_audit_full_{timestamp}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/{designator}/admin/flights", response_class=HTMLResponse)
async def organization_flight_admin(
        request: Request,
        designator: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        db: AsyncSession = Depends(get_db)):
    organization, user = await require_organization_records_admin(
        request,
        designator,
    )
    stmt = apply_date_filter(
        select(Flight).where(Flight.organization_id == organization.id),
        start_date,
        end_date,
    ).order_by(Flight.start_time.desc())
    if not start_date and not end_date:
        stmt = stmt.limit(50)
    result = await db.execute(stmt)
    base_url = f"/{organization.designator.lower()}/admin/flights"
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "request": request,
            "flights": result.scalars().all(),
            "start_date": start_date.isoformat() if start_date else "",
            "end_date": end_date.isoformat() if end_date else "",
            "export_url": organization_flight_admin_url(
                organization.designator,
                start_date,
                end_date,
            ).replace("/admin/flights", "/admin/flights/export", 1),
            "archive_export_url": f"{base_url}/archive",
            "admin_title": f"{organization.designator} Flight Administration",
            "admin_heading": f"{organization.designator} Flight Log Editor",
            "dashboard_url": f"/{organization.designator.lower()}",
            "admin_base_url": base_url,
            "flight_log_base_url": base_url,
            "batch_url": f"{base_url}/batch",
            "import_url": f"{base_url}/import",
            "archive_import_url": f"{base_url}/import-archive",
            "backfill_url": f"{base_url}/backfill-csv",
            "delete_all_url": f"{base_url}/delete",
            "delete_all_label": (
                f"Delete All {organization.designator} Flights "
                "(do you have a recent export/archive to restore from?)"
            ),
            "form_token": csrf_token(request, "organization_records_admin"),
            "organization_page_designator": organization.designator,
            "organization_identity_name": user.display_name,
        },
    )


@app.get("/{designator}/admin/flights/{flight_id}/log", response_class=FileResponse)
async def organization_flight_log_download(
        request: Request,
        designator: str,
        flight_id: int,
        db: AsyncSession = Depends(get_db)):
    organization, _user = await require_organization_records_admin(
        request,
        designator,
    )
    result = await db.execute(
        select(Flight).where(
            Flight.id == flight_id,
            Flight.organization_id == organization.id,
        )
    )
    flight = result.scalar_one_or_none()
    if flight is None or not flight.archive_relpath:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flight log not found.",
        )

    normalized = os.path.normpath(flight.archive_relpath)
    scope_prefix = os.path.join(
        "organizations",
        organization.designator.lower(),
    ) + os.sep
    if os.path.isabs(normalized) or not normalized.startswith(scope_prefix):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flight log not found.",
        )

    filepath = os.path.join(BASE_LOG_DIRECTORY, normalized)
    if not os.path.isfile(filepath):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flight log not found.",
        )
    return FileResponse(
        filepath,
        media_type="application/geo+json",
        filename=os.path.basename(normalized),
    )


@app.get("/{designator}/admin/flights/export", response_class=Response)
async def organization_flight_export(
        request: Request,
        designator: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        db: AsyncSession = Depends(get_db)):
    organization, _user = await require_organization_records_admin(
        request,
        designator,
    )
    stmt = apply_date_filter(
        select(Flight).where(Flight.organization_id == organization.id),
        start_date,
        end_date,
    ).order_by(Flight.start_time)
    result = await db.execute(stmt)
    return organization_admin_csv_response(
        result.scalars().all(),
        organization.designator,
    )


@app.get("/{designator}/admin/flights/archive", response_class=FileResponse)
async def organization_flight_archive_export(
        request: Request,
        designator: str,
        bg_tasks: BackgroundTasks,
        db: AsyncSession = Depends(get_db)):
    organization, _user = await require_organization_records_admin(
        request,
        designator,
    )
    result = await db.execute(
        select(Flight.archive_relpath).where(
            Flight.organization_id == organization.id,
            Flight.archive_relpath != "",
        )
    )
    scope_prefix = f"organizations/{organization.designator.lower()}/"
    archive_files = []
    for relpath in result.scalars().all():
        normalized = os.path.normpath(relpath or "")
        if not normalized.startswith(scope_prefix):
            continue
        filepath = os.path.join(BASE_LOG_DIRECTORY, normalized)
        if os.path.isfile(filepath):
            archive_files.append((filepath, normalized[len(scope_prefix):]))
    if not archive_files:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {organization.designator} flight logs to archive.",
        )

    archive_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_filename = (
        f"r2c-{organization.designator.lower()}-flightlogs-"
        f"{archive_timestamp}.tgz"
    )
    tmp_dir = os.path.join(BASE_LOG_DIRECTORY, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    temp_archive_path = os.path.join(
        tmp_dir,
        f"{secrets.token_hex(8)}-{archive_filename}",
    )
    try:
        with tarfile.open(temp_archive_path, "w:gz") as archive:
            for filepath, archive_name in sorted(archive_files):
                archive.add(filepath, arcname=archive_name)
    except Exception:
        try:
            if os.path.exists(temp_archive_path):
                os.unlink(temp_archive_path)
        except OSError:
            pass
        raise
    bg_tasks.add_task(os.unlink, temp_archive_path)
    return FileResponse(
        temp_archive_path,
        media_type="application/gzip",
        filename=archive_filename,
    )


@app.post("/{designator}/admin/flights/batch")
async def organization_batch_update_flights(
        request: Request,
        designator: str,
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        form_token: Annotated[str, Form()] = "",
        db: AsyncSession = Depends(get_db)):
    organization, _user = await require_organization_records_admin(
        request,
        designator,
    )
    verify_csrf(request, "organization_records_admin", form_token)
    redirect_url = organization_flight_admin_url(
        organization.designator,
        start_date,
        end_date,
    )
    form_data = await request.form()
    action, flight_ids, delete_ids, updates = parse_admin_batch_form(form_data)
    if not flight_ids:
        flash(request, "No flights were submitted.", "info")
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

    result = await db.execute(
        select(Flight).where(
            Flight.id.in_(flight_ids),
            Flight.organization_id == organization.id,
        )
    )
    flights = {flight.id: flight for flight in result.scalars().all()}
    missing_ids = [flight_id for flight_id in flight_ids if flight_id not in flights]

    if action == "delete_selected":
        deleted_ids = []
        for flight_id in flight_ids:
            flight = flights.get(flight_id)
            if flight_id in delete_ids and flight is not None:
                await db.delete(flight)
                deleted_ids.append(flight_id)
        if deleted_ids:
            await db.commit()
            flash(
                request,
                f"Deleted {len(deleted_ids)} flight(s): "
                + ", ".join(str(flight_id) for flight_id in deleted_ids),
                "success",
            )
        else:
            flash(request, "No organization flights were selected for deletion.", "info")
    else:
        changed_ids = []
        for flight_id in flight_ids:
            flight = flights.get(flight_id)
            if flight is None:
                continue
            submitted = updates[flight_id]
            new_sar_id = submitted["sar_id"]
            new_uas = submitted["uas"]
            if flight.sar_id == new_sar_id and flight.uas == new_uas:
                continue
            overlap_result = await find_overlap(
                db,
                flight.start_time,
                flight.end_time,
                remote_id=flight.remote_id,
                sar_id=new_sar_id,
                organization_id=organization.id,
            )
            overlap = overlap_result.scalars().first()
            if overlap and overlap.id != flight.id:
                flash(
                    request,
                    f"Flight {flight_id} edit rejected. Change would overlap "
                    f"with flight record {overlap.id}.",
                    "warning",
                )
                continue
            flight.sar_id = new_sar_id
            flight.uas = new_uas
            changed_ids.append(flight_id)
        if changed_ids:
            await db.commit()
            flash(request, f"Saved changes for {len(changed_ids)} flight(s).", "success")
        else:
            flash(request, "No field changes were detected.", "info")

    if missing_ids:
        flash(
            request,
            "Skipped flights outside this organization or no longer present: "
            + ", ".join(str(flight_id) for flight_id in missing_ids),
            "warning",
        )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/{designator}/admin/flights/delete")
async def organization_reset_flights(
        request: Request,
        designator: str,
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        form_token: Annotated[str, Form()] = "",
        db: AsyncSession = Depends(get_db)):
    organization, _user = await require_organization_records_admin(request, designator)
    verify_csrf(request, "organization_records_admin", form_token)
    await db.execute(delete(Flight).where(Flight.organization_id == organization.id))
    await db.commit()
    flash(request, f"All {organization.designator} flights were deleted.", "success")
    return RedirectResponse(
        url=organization_flight_admin_url(organization.designator, start_date, end_date),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/admin/flights/import")
async def organization_import_csv(
        request: Request,
        designator: str,
        file: UploadFile = File(...),
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        form_token: Annotated[str, Form()] = "",
        db: AsyncSession = Depends(get_db)):
    organization, _user = await require_organization_records_admin(request, designator)
    verify_csrf(request, "organization_records_admin", form_token)
    redirect_url = organization_flight_admin_url(
        organization.designator,
        start_date,
        end_date,
    )
    try:
        reader = csv.DictReader(io.StringIO((await file.read()).decode("utf-8")))
        rows = list(reader)
        if not rows or not rows[0].get("Start Lattitude"):
            raise ValueError("Import requires the full administrator CSV export.")
        for row in rows:
            db.add(Flight(
                organization_id=organization.id,
                sar_id=row.get("Sar Id", "").upper(),
                remote_id=normalize_remote_id(row.get("Remote Id", "")),
                uas=row.get("UAS", "").lower(),
                incident=row.get("Incident", ""),
                op_period=row.get("Op Period", ""),
                map_id=row.get("Map Id", "").upper(),
                start_time=datetime_from_format(row.get("Start Time", None)),
                end_time=datetime_from_format(row.get("End Time", None)),
                start_lat=float(row.get("Start Lattitude", 0.0)),
                start_lng=float(row.get("Start Longitude", 0.0)),
                hours=float(row.get("Hours", 0.0)),
                distance_mi=float(row.get("Distance (mi)", 0.0)),
                temp_f=float(row.get("Temp (F)", 0.0)),
                rhum_pct=float(row.get("Rel Humidity (%)", 0.0)),
                dewpt_f=float(row.get("Dew Pt (F)", 0.0)),
                precip_in=float(row.get("Precip (in)", 0.0)),
                wind_mph=float(row.get("Wind (mph)", 0.0)),
                gusts_mph=float(row.get("Gusts (mph)", 0.0)),
                cloudcvr_pct=float(row.get("Cloud Cover (%)", 0.0)),
                timeofday=row.get("Time Of Day", ""),
                archive_relpath="",
            ))
        await db.commit()
        flash(request, f"Imported {len(rows)} {organization.designator} flight(s) from CSV.", "success")
    except Exception as exc:
        await db.rollback()
        flash(request, f"CSV import failed: {exc}", "warning")
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/{designator}/admin/flights/backfill-csv")
async def organization_backfill_csv(
        request: Request,
        designator: str,
        file: UploadFile = File(...),
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        form_token: Annotated[str, Form()] = "",
        db: AsyncSession = Depends(get_db)):
    organization, _user = await require_organization_records_admin(request, designator)
    verify_csrf(request, "organization_records_admin", form_token)
    redirect_url = organization_flight_admin_url(
        organization.designator,
        start_date,
        end_date,
    )
    rows = list(csv.DictReader(io.StringIO((await file.read()).decode("utf-8"))))
    if not rows or not rows[0].get("Start Lattitude"):
        flash(request, "Backfill requires a non-empty full administrator CSV export.", "warning")
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

    result = await db.execute(
        select(Flight).where(Flight.organization_id == organization.id)
    )
    flights = result.scalars().all()
    unmatched_flights = list(flights)
    used_flight_ids = set()
    flight_lookup = {}
    for flight in flights:
        key = (
            normalize_csv_value(flight.sar_id).upper(),
            normalize_remote_id(flight.remote_id),
            normalize_csv_value(flight.uas).lower(),
            normalize_match_datetime(flight.start_time),
            normalize_match_datetime(flight.end_time),
        )
        flight_lookup.setdefault(key, []).append(flight)

    updated_count = 0
    missing_count = 0
    for row in rows:
        start_time = datetime_from_format(row.get("Start Time", None))
        end_time = datetime_from_format(row.get("End Time", None))
        key = (
            normalize_csv_value(row.get("Sar Id", "")).upper(),
            normalize_remote_id(row.get("Remote Id", "")),
            normalize_csv_value(row.get("UAS", "")).lower(),
            normalize_match_datetime(start_time),
            normalize_match_datetime(end_time),
        )
        matches = flight_lookup.get(key, [])
        while matches and matches[0].id in used_flight_ids:
            matches.pop(0)
        flight = matches.pop(0) if matches else None
        if flight is None:
            csv_start_lat = parse_csv_float(row.get("Start Lattitude", 0.0))
            csv_start_lng = parse_csv_float(row.get("Start Longitude", 0.0))
            candidates = [
                candidate for candidate in unmatched_flights
                if datetime_match_within_seconds(candidate.start_time, start_time)
                and datetime_match_within_seconds(candidate.end_time, end_time)
                and coordinates_match(
                    candidate.start_lat,
                    candidate.start_lng,
                    csv_start_lat,
                    csv_start_lng,
                )
            ]
            flight = candidates[0] if len(candidates) == 1 else None
        if flight is None:
            missing_count += 1
            continue
        used_flight_ids.add(flight.id)
        if flight in unmatched_flights:
            unmatched_flights.remove(flight)
        flight.incident = normalize_csv_value(row.get("Incident", ""))
        flight.op_period = normalize_csv_value(row.get("Op Period", ""))
        remote_id = normalize_remote_id(row.get("Remote Id", ""))
        if remote_id:
            flight.remote_id = remote_id
        flight.map_id = normalize_csv_value(row.get("Map Id", "")).upper()
        flight.start_lat = parse_csv_float(row.get("Start Lattitude", 0.0))
        flight.start_lng = parse_csv_float(row.get("Start Longitude", 0.0))
        flight.hours = parse_csv_float(row.get("Hours", 0.0))
        flight.distance_mi = parse_csv_float(row.get("Distance (mi)", 0.0))
        flight.temp_f = parse_csv_float(row.get("Temp (F)", 0.0))
        flight.rhum_pct = parse_csv_float(row.get("Rel Humidity (%)", 0.0))
        flight.dewpt_f = parse_csv_float(row.get("Dew Pt (F)", 0.0))
        flight.precip_in = parse_csv_float(row.get("Precip (in)", 0.0))
        flight.wind_mph = parse_csv_float(row.get("Wind (mph)", 0.0))
        flight.gusts_mph = parse_csv_float(row.get("Gusts (mph)", 0.0))
        flight.cloudcvr_pct = parse_csv_float(row.get("Cloud Cover (%)", 0.0))
        flight.timeofday = normalize_csv_value(row.get("Time Of Day", ""), "day")
        updated_count += 1
    await db.commit()
    flash(request, f"Backfilled {updated_count} {organization.designator} flight(s) from CSV.", "success")
    if missing_count:
        flash(request, f"Could not match {missing_count} CSV row(s).", "warning")
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/{designator}/admin/flights/import-archive")
async def organization_import_flight_archive(
        request: Request,
        designator: str,
        file: UploadFile = File(...),
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        form_token: Annotated[str, Form()] = "",
        db: AsyncSession = Depends(get_db)):
    organization, _user = await require_organization_records_admin(request, designator)
    verify_csrf(request, "organization_records_admin", form_token)
    redirect_url = organization_flight_admin_url(
        organization.designator,
        start_date,
        end_date,
    )
    existing_count = await db.scalar(
        select(func.count()).select_from(Flight).where(
            Flight.organization_id == organization.id
        )
    )
    if existing_count:
        flash(
            request,
            f"Archive import requires {organization.designator} to have no flights; other organizations are unaffected.",
            "warning",
        )
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

    written_paths = []
    skipped_files = []
    try:
        archive_bytes = io.BytesIO(await read_upload_with_limit(file))
        with tarfile.open(fileobj=archive_bytes, mode="r:*") as tar:
            members = reviewed_flight_archive_members(tar)
            for member in members:
                extracted = tar.extractfile(member)
                if extracted is None:
                    skipped_files.append(f"{member.name}: unreadable")
                    continue
                try:
                    async with db.begin_nested():
                        data = json.load(extracted)
                        flight_inputs = await extract_flight_inputs_from_geojson(data)
                        _flight, archive_path = await create_imported_flight_and_archive(
                            db,
                            data,
                            flight_inputs,
                            organization_id=organization.id,
                            organization_designator=organization.designator,
                        )
                    written_paths.append(archive_path)
                except Exception as exc:
                    skipped_files.append(f"{member.name}: {exc}")
        await db.commit()
    except Exception as exc:
        await db.rollback()
        for archive_path in written_paths:
            try:
                if os.path.exists(archive_path):
                    os.unlink(archive_path)
            except OSError:
                pass
        flash(request, f"Archive import failed: {exc}", "warning")
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

    imported_count = len(written_paths)
    if imported_count:
        flash(
            request,
            f"Imported {imported_count} {organization.designator} flight log(s) from archive.",
            "success",
        )
    else:
        flash(request, "No flight logs were imported from the archive.", "warning")
    if skipped_files:
        flash(
            request,
            f"Skipped {len(skipped_files)} file(s). First issue: {skipped_files[0]}",
            "warning",
        )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/r2c-thumbnail/{tablet_code}/{session_id}.jpg")
async def r2c_transient_video_thumbnail(
        tablet_code: str,
        session_id: str):
    """Return a current tablet JPEG without persisting it in tracker storage.

    This endpoint is intentionally capability-addressed rather than
    session-authenticated because CalTopo fetches camera.thumbnail_url itself.
    Both locators are high-entropy, the stream must still be actively
    advertised, and every response is non-cacheable.
    """
    tablet = await r2c_hub.resolve_tablet_link_code(tablet_code)
    if tablet is None:
        raise HTTPException(status_code=404, detail="Thumbnail not found.")
    streams = await control_plane_store.list_active_video_streams(
        tablet.organization_id
    )
    stream = next((
        item for item in streams
        if item.device_credential_id == tablet.id
        and secrets.compare_digest(item.session_id, session_id)
    ), None)
    if stream is None or not stream.thumbnail_revision:
        raise HTTPException(status_code=404, detail="Thumbnail not found.")
    jpeg = await r2c_hub.video_thumbnail(
        device_credential_id=tablet.id,
        session_id=stream.session_id,
        revision=stream.thumbnail_revision,
    )
    if jpeg is None:
        raise HTTPException(status_code=404, detail="Thumbnail not available.")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, noimageindex",
        },
    )


@app.get("/{designator}/streams", response_class=HTMLResponse)
async def organization_streams(
        request: Request,
        designator: str,
        tablet: str = "",
        stream: str = "",
        session: str = ""):
    clean_tablet_code = tablet.strip()
    clean_stream = stream.strip()
    clean_session = session.strip()
    tablet_device = None
    if clean_tablet_code:
        tablet_device = await r2c_hub.resolve_tablet_link_code(
            clean_tablet_code
        )
        if tablet_device is None:
            raise HTTPException(status_code=404, detail="R2C tablet not found.")
    organization, user = await require_organization_user(
        request,
        designator,
        ("video_requester",),
        redirect_to_login=True,
        login_next=(
            f"/{designator.lower()}/streams/"
            f"{quote(tablet_device.device_name, safe='')}"
            + (
                f"/{quote(clean_stream, safe='')}"
                if clean_stream
                else ""
            )
            if tablet_device is not None
            else ""
        ),
    )
    if tablet_device is not None:
        if (
            tablet_device.organization_id != organization.id
        ):
            raise HTTPException(status_code=404, detail="R2C tablet not found.")

    (
        streams,
        requests,
        organization_requests,
        video_ice_servers,
        connected_tablets,
        recording_download_requests,
        beta_allowance,
    ) = await asyncio.gather(
            control_plane_store.list_active_video_streams(organization.id),
            control_plane_store.list_video_stream_requests(
                organization_id=organization.id,
                requester_user_id=user.id,
            ),
            control_plane_store.list_video_stream_requests(
                organization_id=organization.id,
            ),
            video_ice_server_provider.get_ice_servers(
                f"organization:{organization.id}"
            ),
            r2c_hub.list_connected_tablets(organization.id),
            control_plane_store.list_recording_download_requests(
                organization_id=organization.id,
            ),
            control_plane_store.get_extended_beta_allowance(organization.id),
        )
    if tablet_device is not None:
        streams = tuple(
            stream for stream in streams
            if stream.device_credential_id == tablet_device.id
        )
        requests = tuple(
            stream_request for stream_request in requests
            if stream_request.device_credential_id == tablet_device.id
        )
        organization_requests = tuple(
            stream_request for stream_request in organization_requests
            if stream_request.device_credential_id == tablet_device.id
        )
    if clean_stream:
        streams = tuple(
            item for item in streams
            if item.drone_designator.strip().lower() == clean_stream.lower()
        )
        if not streams:
            raise HTTPException(status_code=404, detail="Captured stream not found.")
    if clean_session:
        streams = tuple(
            item for item in streams
            if item.session_id == clean_session and item.media_kind == "recording"
        )
        if not streams:
            raise HTTPException(status_code=404, detail="Captured recording not found.")
    session_preflight = request.session.pop(
        "organization_video_preflight_request", None
    )
    session_preflight_id = ""
    if (
        isinstance(session_preflight, dict)
        and session_preflight.get("designator") == organization.designator
    ):
        session_preflight_id = str(session_preflight.get("request_id", ""))
    requested_preflight_id = (
        request.query_params.get("preflight", "").strip()
        or session_preflight_id.strip()
    )
    active_preflight_request_id = next(
        (
            stream_request.id
            for stream_request in requests
            if stream_request.id == requested_preflight_id
            and stream_request.state in {"pending", "probing"}
        ),
        "",
    )
    active_media_request = next(
        (
            stream_request
            for stream_request in requests
            if stream_request.state in {"approved", "streaming"}
        ),
        None,
    )
    active_media_request_id = (
        active_media_request.id if active_media_request is not None else ""
    )
    request_in_progress_session_ids = {
        stream_request.stream_session_id
        for stream_request in organization_requests
        if stream_request.state in {
            "pending",
            "probing",
            "awaiting_approval",
            "approved",
            "streaming",
        }
        and stream_request.expires_at >= datetime.now(UTC)
    }
    cancellable_request_by_session = {
        stream_request.stream_session_id: stream_request
        for stream_request in requests
        if stream_request.state in {"pending", "probing", "awaiting_approval"}
        and stream_request.expires_at >= datetime.now(UTC)
    }
    active_consumers_by_device_id = {
        stream_request.device_credential_id: stream_request.requester_email
        for stream_request in organization_requests
        if stream_request.state in {"approved", "streaming"}
    }
    stream_status = organization_stream_status(
        streams,
        organization_requests,
    )
    remote_control_enabled = (
        any(stream.remote_control_enabled for stream in streams)
        or await r2c_hub.remote_video_control_enabled(
            organization_id=organization.id,
            device_credential_id=(
                tablet_device.id if tablet_device is not None else ""
            ),
        )
    )
    return templates.TemplateResponse(
        request=request,
        name="organization_streams.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "organization": organization,
            "organization_user": user,
            "streams": streams,
            "stream_tablet_codes": {
                stream.session_id: tablet_link_code(
                    organization.designator,
                    stream.device_name,
                )
                for stream in streams
            },
            "stream_requests": requests,
            "csrf_token": csrf_token(request, "organization_streams"),
            "logout_csrf_token": csrf_token(request, "organization_admin"),
            "organization_page_designator": organization.designator,
            "organization_identity_name": user.display_name,
            "tablet_device": tablet_device,
            "connected_tablets": connected_tablets,
            "tablet_code": clean_tablet_code,
            "tablet_device_id": (
                tablet_device.id if tablet_device is not None else ""
            ),
            "stream_filter": clean_stream,
            "active_preflight_request_id": active_preflight_request_id,
            "active_media_request_id": active_media_request_id,
            "active_media_request": active_media_request,
            "request_in_progress_session_ids": (
                request_in_progress_session_ids
            ),
            "cancellable_request_by_session": cancellable_request_by_session,
            "active_consumers_by_device_id": active_consumers_by_device_id,
            "remote_control_enabled": remote_control_enabled,
            "recording_downloads_enabled": RECORDING_DOWNLOADS_ENABLED,
            "video_streaming_allowed": (
                beta_allowance is None or beta_allowance.video_streaming_allowed
            ),
            "video_disabled_month_end": (
                beta_allowance.month_ends_at
                if beta_allowance is not None
                and not beta_allowance.video_streaming_allowed
                else None
            ),
            "recording_downloads_by_session": {
                item.stream_session_id: item
                for item in recording_download_requests
            },
            "video_ice_servers": video_ice_servers,
            "stream_status_active": stream_status["active"],
            "stream_membership_revision": stream_status[
                "membership_revision"
            ],
        },
        headers={
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


def organization_stream_status(streams, requests) -> dict:
    """Return privacy-minimal lifecycle state and its next expiry boundary."""
    stream_values = [
        {
            "id": stream.id,
            "device": stream.device_name,
            "incident": stream.incident_name,
            "drone": stream.drone_designator,
            "width": stream.source_width,
            "height": stream.source_height,
            "fps": stream.source_fps,
            "bitrate": stream.source_bitrate_bps,
            "codec": stream.source_codec,
            "kind": stream.media_kind,
            "recordedAt": (
                stream.recorded_at.isoformat()
                if stream.recorded_at is not None else ""
            ),
            "durationMs": stream.duration_ms,
            "thumbnailRevision": stream.thumbnail_revision,
            "remoteControlEnabled": stream.remote_control_enabled,
        }
        for stream in streams
    ]
    request_values = [
        {
            "id": stream_request.id,
            "device": stream_request.device_name,
            "state": stream_request.state,
            "statusMessage": getattr(stream_request, "status_message", ""),
            "route": stream_request.route_kind,
            "uplink": stream_request.estimated_uplink_bps,
            "selectedWidth": stream_request.selected_width,
            "selectedHeight": stream_request.selected_height,
            "selectedFps": stream_request.selected_fps,
            "selectedBitrate": stream_request.selected_bitrate_bps,
        }
        for stream_request in requests
    ]
    encoded = json.dumps(
        {"streams": stream_values, "requests": request_values},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    nonterminal_states = {
        "pending", "probing", "awaiting_approval", "approved", "streaming"
    }
    expiry_boundaries = [stream.expires_at for stream in streams]
    expiry_boundaries.extend(
        item.expires_at
        for item in requests
        if item.state in nonterminal_states
    )
    return {
        "revision": hashlib.sha256(encoded).hexdigest()[:20],
        "membership_revision": stream_membership_revision(streams),
        # Passive page refresh is driven by an advertised R2C stream, not by a
        # request that can outlive the tablet which advertised it.  Explicit
        # preflight/media scripts continue to own their active operations.
        "active": bool(streams),
        "awaiting_approval": any(
            item.state == "awaiting_approval" for item in requests
        ),
        "next_expiry": min(expiry_boundaries) if expiry_boundaries else None,
    }


def stream_membership_revision(streams) -> str:
    """Identify the rendered stream set without volatile source telemetry."""
    encoded = json.dumps(
        sorted(stream.session_id for stream in streams),
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def thumbnail_preview_device_ids(streams, scoped_device_id: str = "") -> tuple[str, ...]:
    """Return the tablets whose visible live thumbnails need a refresh lease."""
    scoped = scoped_device_id.strip()
    device_ids = {
        stream.device_credential_id
        for stream in streams
        if stream.media_kind == "live"
        and stream.device_credential_id
        and (not scoped or stream.device_credential_id == scoped)
    }
    return tuple(sorted(device_ids))


@app.post("/{designator}/streams/{session_id}/request")
async def organization_request_stream(
        request: Request,
        designator: str,
        session_id: str,
        form_token: Annotated[str, Form()],
        tablet: Annotated[str, Form()] = "",
        stream: Annotated[str, Form()] = ""):
    verify_csrf(request, "organization_streams", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("video_requester",),
    )
    preflight_request_id = ""
    try:
        stream_request = await control_plane_store.create_video_stream_request(
            organization_id=organization.id,
            stream_session_id=session_id,
            requester_user_id=user.id,
        )
        delivered = await r2c_hub.send_video_stream_request(
            device_credential_id=stream_request.device_credential_id,
            request_id=stream_request.id,
            requester_email=stream_request.requester_email,
            stream_session_id=stream_request.stream_session_id,
            incident_name=stream_request.incident_name,
            drone_designator=stream_request.drone_designator,
            source_width=stream_request.source_width,
            source_height=stream_request.source_height,
            source_fps=stream_request.source_fps,
            source_bitrate_bps=stream_request.source_bitrate_bps,
            source_codec=stream_request.source_codec,
            remote_control_enabled=stream_request.remote_control_enabled,
            expires_at=stream_request.expires_at,
        )
        preflight_request_id = stream_request.id
        request.session["organization_video_preflight_request"] = {
            "designator": organization.designator,
            "request_id": stream_request.id,
        }
        flash(request, (
            "Stream request sent to the drone team. " if delivered
            else "Stream request recorded; device delivery is pending. "
        ) + (
            "You will choose the measured video quality."
            if stream_request.remote_control_enabled
            else "Video will remain off until the pilot or visual observer approves it."
        ), "success")
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=((
            f"/{organization.designator.lower()}/streams/"
            f"{quote(stream_request.device_name, safe='')}"
            + (f"/{quote(stream.strip(), safe='')}" if stream.strip() else "")
            if preflight_request_id else f"/{organization.designator.lower()}/streams"
        ) + (f"?{urlencode({'preflight': preflight_request_id})}" if preflight_request_id else "")),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/streams/{session_id}/download-request")
async def organization_request_recording_download(
        request: Request, designator: str, session_id: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_streams", form_token)
    organization, user = await require_organization_user(
        request, designator, ("video_requester",)
    )
    if not RECORDING_DOWNLOADS_ENABLED:
        raise HTTPException(status_code=404, detail="Recording downloads are not enabled.")
    return_device_name = ""
    try:
        item = await control_plane_store.create_recording_download_request(
            organization_id=organization.id,
            stream_session_id=session_id,
            requester_user_id=user.id,
        )
        delivered = (
            True if item.state == "ready"
            else await r2c_hub.send_recording_download_request(item)
        )
        return_device_name = item.device_name
        flash(request, (
            "Recording is already available to download."
            if item.state == "ready"
            else "Recording transfer authorized and sent to the tablet."
            if item.remote_control_enabled
            else "Recording transfer requested; the tablet operator must approve it."
        ) if delivered else "Recording transfer queued for the tablet.", "success")
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=(
            f"/{organization.designator.lower()}/streams/"
            f"{quote(return_device_name, safe='')}"
            if return_device_name
            else f"/{organization.designator.lower()}/streams"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.put("/recording-downloads/{request_id}/content")
async def upload_recording_download(
        request: Request, request_id: str,
        credential: Optional[DeviceCredentialRecord] = Depends(get_api_key)):
    if credential is None:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Device credential required")
    if not RECORDING_DOWNLOADS_ENABLED:
        raise HTTPException(status_code=404, detail="Recording downloads are not enabled.")
    try:
        item = await control_plane_store.get_recording_download_request(
            request_id=request_id, device_credential_id=credential.id,
        )
    except ControlPlaneError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if item.state == "awaiting_approval":
        item = await control_plane_store.decide_recording_download_request(
            request_id=request_id, device_credential_id=credential.id, approved=True,
        )
        logger.info(
            "Recording upload supplied operator approval: request=%s device=%s",
            request_id,
            credential.id,
        )
    if item.state not in {"approved", "uploading"}:
        raise HTTPException(status_code=409, detail="Recording transfer is not authorized.")
    try:
        item = await control_plane_store.mark_recording_download_uploading(
            request_id=request_id,
            device_credential_id=credential.id,
        )
    except ControlPlaneError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raw_filename = request.headers.get("X-R2C-Filename", "recording.mp4")
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(raw_filename))[:240]
    if not filename:
        filename = "recording.mp4"
    media_type = (request.headers.get("Content-Type", "video/mp4") or "video/mp4")[:120]
    relative_dir = os.path.join(
        "organizations", credential.designator.lower(), "recordings", item.stream_session_id,
    )
    destination_dir = os.path.join(BASE_LOG_DIRECTORY, relative_dir)
    os.makedirs(destination_dir, exist_ok=True)
    destination = os.path.join(destination_dir, filename)
    max_bytes = 16 * 1024 * 1024 * 1024
    content_range = request.headers.get("Content-Range", "").strip()
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
    if content_range and match is None:
        raise HTTPException(status_code=400, detail="Invalid Content-Range header.")
    range_start, range_end, total_bytes = (
        tuple(map(int, match.groups())) if match else (0, -1, -1)
    )
    if range_start == 0:
        logger.info(
            "Recording upload started: request=%s device=%s total_bytes=%s",
            request_id,
            credential.id,
            total_bytes,
        )
    if match and (
        range_end < range_start or total_bytes <= range_end or total_bytes > max_bytes
        or range_end - range_start + 1 > 16 * 1024 * 1024
    ):
        raise HTTPException(status_code=400, detail="Invalid recording chunk range.")
    partial_path = os.path.join(destination_dir, f".{request_id}.part")
    if range_start > 0 and not os.path.isfile(partial_path):
        raise HTTPException(status_code=409, detail="Recording upload must restart at byte zero.")
    mode = "wb" if range_start == 0 else "r+b"
    received = 0
    async with await anyio.open_file(partial_path, mode) as output:
        if range_start:
            await output.seek(range_start)
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if (match and received > range_end - range_start + 1) or (
                not match and received > max_bytes
            ):
                raise HTTPException(status_code=413, detail="Recording chunk is too large.")
            await output.write(chunk)
    if received == 0:
        raise HTTPException(status_code=400, detail="Recording upload was empty.")
    if match and received != range_end - range_start + 1:
        raise HTTPException(status_code=400, detail="Recording chunk length did not match Content-Range.")
    if match and range_end + 1 < total_bytes:
        return {"accepted": True, "state": "uploading", "bytes": range_end + 1}
    byte_count = total_bytes if match else received
    os.replace(partial_path, destination)
    digest = hashlib.sha256()
    async with await anyio.open_file(destination, "rb") as completed_file:
        while chunk := await completed_file.read(1024 * 1024):
            digest.update(chunk)
    storage_relpath = os.path.join(relative_dir, filename)
    try:
        completed = await control_plane_store.complete_recording_download_upload(
            request_id=request_id, device_credential_id=credential.id,
            filename=filename, media_type=media_type, byte_count=byte_count,
            sha256=digest.hexdigest(), storage_relpath=storage_relpath,
            spool_ttl_seconds=RECORDING_DOWNLOAD_SPOOL_TTL_SEC,
        )
    except ControlPlaneError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "Recording upload completed: request=%s device=%s bytes=%s",
        request_id,
        credential.id,
        completed.byte_count,
    )
    return {"accepted": True, "state": completed.state, "bytes": completed.byte_count}


async def authorized_recording_copy(
        organization, request_id: str):
    try:
        item = await control_plane_store.get_recording_download_request(
            request_id=request_id, organization_id=organization.id,
        )
    except ControlPlaneError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if item.state != "ready" or not item.storage_relpath:
        raise HTTPException(status_code=409, detail="Recording is not ready to download.")
    normalized = os.path.normpath(item.storage_relpath)
    scope = os.path.join("organizations", organization.designator.lower(), "recordings") + os.sep
    if os.path.isabs(normalized) or not normalized.startswith(scope):
        raise HTTPException(status_code=404, detail="Recording file not found.")
    path = os.path.join(BASE_LOG_DIRECTORY, normalized)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Recording file not found.")
    return item, path


async def delete_delivered_recording_copy(request_id: str, path: str) -> None:
    """Remove a one-shot transfer spool after its streamed response completes."""
    try:
        await control_plane_store.complete_recording_download_delivery(
            request_id=request_id,
        )
    except Exception as exc:
        logger.warning("Unable to mark recording transfer %s delivered: %s", request_id, exc)
        return
    try:
        await anyio.to_thread.run_sync(os.remove, path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Unable to remove recording transfer spool %s: %s", request_id, exc)


async def stream_recording_copy(item, path: str):
    """Yield a temporary recording without triggering Cloud Run response buffering."""
    sent = 0
    completed = False
    try:
        async with await anyio.open_file(path, "rb") as source:
            while chunk := await source.read(1024 * 1024):
                sent += len(chunk)
                yield chunk
        completed = sent == item.byte_count
    finally:
        if sent:
            await meter_organization_usage_by_id(
                item.organization_id,
                network_bytes=sent,
            )
        if completed:
            completed_at = item.completed_at or datetime.now(UTC)
            retained_seconds = max(
                0,
                int((datetime.now(UTC) - completed_at).total_seconds()),
            )
            storage_byte_days = max(
                0,
                round(item.byte_count * retained_seconds / 86_400),
            )
            if storage_byte_days:
                await meter_organization_usage_by_id(
                    item.organization_id,
                    storage_byte_days=storage_byte_days,
                )
            await delete_delivered_recording_copy(item.id, path)


@app.get("/{designator}/streams/downloads/{request_id}", response_class=StreamingResponse)
async def organization_recording_download(
        request: Request, designator: str, request_id: str):
    organization, _user = await require_organization_user(
        request, designator, ("video_requester",)
    )
    if not RECORDING_DOWNLOADS_ENABLED:
        raise HTTPException(status_code=404, detail="Recording downloads are not enabled.")
    if request.headers.get("Range"):
        raise HTTPException(
            status_code=416,
            detail="Recording transfer downloads must be requested as a complete file.",
        )
    item, path = await authorized_recording_copy(organization, request_id)
    safe_filename = re.sub(r'["\\\r\n]+', "_", item.filename or "recording.mp4")
    return StreamingResponse(
        stream_recording_copy(item, path),
        media_type=item.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                f'attachment; filename="{safe_filename}"; '
                f"filename*=UTF-8''{quote(item.filename or 'recording.mp4', safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/{designator}/streams/downloads/{request_id}/status")
async def organization_recording_download_status(
        request: Request, designator: str, request_id: str):
    organization, user = await require_organization_user(
        request, designator, ("video_requester",)
    )
    if not RECORDING_DOWNLOADS_ENABLED:
        raise HTTPException(status_code=404, detail="Recording downloads are not enabled.")
    try:
        item = await control_plane_store.get_recording_download_request(
            request_id=request_id, organization_id=organization.id,
        )
    except ControlPlaneError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(
        {"requestId": item.id, "state": item.state, "statusMessage": item.status_message},
        headers={"Cache-Control": "private, no-store"},
    )


@app.post("/{designator}/streams/requests/{request_id}/cancel")
async def organization_cancel_stream_request(
        request: Request,
        designator: str,
        request_id: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_streams", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("video_requester",),
    )
    try:
        stream_request = await control_plane_store.cancel_video_stream_request(
            request_id=request_id,
            organization_id=organization.id,
            requester_user_id=user.id,
        )
        delivered = await r2c_hub.send_video_stream_request_cancelled(
            device_credential_id=stream_request.device_credential_id,
            request_id=stream_request.id,
        )
        flash(
            request,
            (
                "Stream request cancelled and the tablet was notified."
                if delivered
                else "Stream request cancelled. The tablet is currently offline."
            ),
            "success",
        )
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/streams",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    "/{designator}/streams/requests/{request_id}/preflight/offer"
)
async def organization_start_video_preflight(
        request: Request,
        designator: str,
        request_id: str,
        payload: BrowserVideoPreflightOffer):
    verify_csrf(request, "organization_streams", payload.form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("video_requester",),
    )
    try:
        exchange = await control_plane_store.start_video_preflight(
            request_id=request_id,
            organization_id=organization.id,
            requester_user_id=user.id,
            browser_offer_sdp=payload.sdp,
            relay_candidate_ms=payload.relay_candidate_ms,
        )
        delivered = await r2c_hub.send_video_preflight_offer(exchange)
        return {
            "accepted": True,
            "delivered": delivered,
            "state": exchange.state,
        }
    except ControlPlaneError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@app.get(
    "/{designator}/streams/requests/{request_id}/preflight/status"
)
async def organization_video_preflight_status(
        request: Request,
        designator: str,
        request_id: str):
    organization, user = await require_organization_user(
        request,
        designator,
        ("video_requester",),
    )
    try:
        exchange = (
            await control_plane_store
            .get_video_preflight_exchange_for_requester(
                request_id=request_id,
                organization_id=organization.id,
                requester_user_id=user.id,
            )
        )
        return {
            "requestId": exchange.request_id,
            "state": exchange.state,
            "statusMessage": exchange.status_message,
            # Long-lived tablet WebSockets can remain attached to the previous
            # Cloud Run revision during a rolling deployment.  Normalize again
            # at the browser boundary so an answer written by an older revision
            # is still safe for the current browser to consume.
            "answerSdp": (
                normalize_video_preflight_answer(exchange.device_answer_sdp)
                if exchange.device_answer_sdp
                else ""
            ),
            "routeKind": exchange.route_kind,
            "estimatedUplinkBps": exchange.estimated_uplink_bps,
            "remoteControlEnabled": exchange.remote_control_enabled,
            "qualityChoices": (
                managed_video_quality_choices(
                    source_width=exchange.source_width,
                    source_height=exchange.source_height,
                    source_fps=exchange.source_fps,
                    usable_uplink_bps=exchange.estimated_uplink_bps,
                )
                if exchange.remote_control_enabled
                and exchange.state == "awaiting_approval"
                else ()
            ),
            "expiresAt": exchange.expires_at.isoformat(),
        }
    except ControlPlaneError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@app.post(
    "/{designator}/streams/requests/{request_id}/remote-control/approve"
)
async def organization_remote_control_video_selection(
        request: Request,
        designator: str,
        request_id: str,
        payload: BrowserVideoQualitySelection):
    verify_csrf(request, "organization_streams", payload.form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("video_requester",),
    )
    try:
        result = await control_plane_store.record_video_stream_decision(
            request_id=request_id,
            requester_user_id=user.id,
            organization_id=organization.id,
            decision="approve",
            selected_width=payload.width,
            selected_height=payload.height,
            selected_fps=payload.fps,
            selected_bitrate_bps=payload.bitrate_bps,
        )
        logger.info(
            "Managed video remote decision accepted: request=%s user=%s "
            "state=%s profile=%sx%s@%s bitrate=%s",
            result.id,
            user.id,
            result.state,
            payload.width,
            payload.height,
            payload.fps,
            payload.bitrate_bps,
        )
        return {"accepted": True, "state": result.state}
    except ControlPlaneError as exc:
        logger.warning(
            "Managed video remote decision rejected: request=%s user=%s error=%s",
            request_id,
            user.id,
            exc,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/{designator}/streams/requests/{request_id}/media/offer")
async def organization_start_video_media(
        request: Request, designator: str, request_id: str,
        payload: BrowserVideoMediaOffer):
    verify_csrf(request, "organization_streams", payload.form_token)
    organization, user = await require_organization_user(
        request, designator, ("video_requester",)
    )
    try:
        logger.info(
            "Managed video browser offer received: request=%s user=%s "
            "relayCandidateMs=%s sdpBytes=%s",
            request_id,
            user.id,
            payload.relay_candidate_ms,
            len(payload.sdp.encode("utf-8")),
        )
        exchange = await control_plane_store.start_video_media(
            request_id=request_id,
            organization_id=organization.id,
            requester_user_id=user.id,
            browser_offer_sdp=payload.sdp,
            relay_candidate_ms=payload.relay_candidate_ms,
        )
        delivered = await r2c_hub.send_video_media_offer(exchange)
        logger.info(
            "Managed video browser offer recorded: request=%s user=%s "
            "state=%s delivered=%s",
            request_id,
            user.id,
            exchange.state,
            delivered,
        )
        return {"accepted": True, "delivered": delivered, "state": exchange.state}
    except ControlPlaneError as exc:
        logger.warning(
            "Managed video browser offer rejected: request=%s user=%s error=%s",
            request_id,
            user.id,
            exc,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/{designator}/streams/requests/{request_id}/media/status")
async def organization_video_media_status(
        request: Request, designator: str, request_id: str):
    organization, user = await require_organization_user(
        request, designator, ("video_requester",)
    )
    try:
        stream_request = await control_plane_store.get_video_stream_request_for_requester(
            request_id=request_id,
            organization_id=organization.id,
            requester_user_id=user.id,
        )
        try:
            exchange = await control_plane_store.get_video_media_exchange_for_requester(
                request_id=request_id,
                organization_id=organization.id,
                requester_user_id=user.id,
            )
            answer_sdp = exchange.device_answer_sdp
            expires_at = exchange.expires_at
        except ControlPlaneError:
            # Terminal transitions intentionally delete the SDP exchange. Keep
            # the durable request state available so the browser can display
            # the device's actual stop reason instead of a generic 409.
            answer_sdp = ""
            expires_at = stream_request.expires_at
        return {
            "requestId": stream_request.id,
            "state": stream_request.state,
            "statusMessage": stream_request.status_message,
            "answerSdp": answer_sdp,
            "expiresAt": expires_at.isoformat(),
        }
    except ControlPlaneError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/{designator}/streams/requests/{request_id}/media/started")
async def organization_video_media_started(
        request: Request, designator: str, request_id: str,
        payload: BrowserVideoMediaState):
    verify_csrf(request, "organization_streams", payload.form_token)
    organization, user = await require_organization_user(
        request, designator, ("video_requester",)
    )
    try:
        result = await control_plane_store.mark_video_streaming(
            request_id=request_id,
            organization_id=organization.id,
            requester_user_id=user.id,
        )
        return {"accepted": True, "state": result.state}
    except ControlPlaneError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/{designator}/streams/requests/{request_id}/media/ended")
async def organization_video_media_ended(
        request: Request, designator: str, request_id: str,
        payload: BrowserVideoMediaState):
    verify_csrf(request, "organization_streams", payload.form_token)
    organization, user = await require_organization_user(
        request, designator, ("video_requester",)
    )
    try:
        result = await control_plane_store.stop_video_stream(
            request_id=request_id,
            organization_id=organization.id,
            requester_user_id=user.id,
            reason=payload.reason,
        )
        await r2c_hub.send_video_stream_request_cancelled(
            device_credential_id=result.device_credential_id,
            request_id=result.id,
        )
        return {"accepted": True, "state": result.state}
    except ControlPlaneError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/{designator}/streams/requests/{request_id}/media/metrics")
async def organization_video_media_metrics(
        request: Request, designator: str, request_id: str,
        payload: BrowserVideoMediaMetrics):
    verify_csrf(request, "organization_streams", payload.form_token)
    organization, user = await require_organization_user(
        request, designator, ("video_requester",)
    )
    try:
        result = await control_plane_store.record_video_media_metrics(
            request_id=request_id,
            organization_id=organization.id,
            requester_user_id=user.id,
            metrics_session_id=payload.metrics_session_id,
            audio_bytes_sent=payload.audio_bytes_sent,
            audio_bytes_received=payload.audio_bytes_received,
            video_bytes_received=payload.video_bytes_received,
        )
        logger.info(
            "Managed video browser diagnostics: request=%s session=%s event=%s "
            "detail=%s peer=%s ice=%s gathering=%s signaling=%s track=%s "
            "elementReady=%s paused=%s element=%sx%s packets=%s bytes=%s "
            "framesReceived=%s framesDecoded=%s framesPresented=%s "
            "framesDropped=%s keyFrames=%s "
            "codec=%s decoder=%s",
            request_id,
            payload.metrics_session_id,
            payload.diagnostic_event,
            payload.diagnostic_detail,
            payload.peer_connection_state,
            payload.ice_connection_state,
            payload.ice_gathering_state,
            payload.signaling_state,
            payload.video_track_state,
            payload.video_element_ready_state,
            payload.video_element_paused,
            payload.video_element_width,
            payload.video_element_height,
            payload.video_packets_received,
            payload.video_bytes_received,
            payload.video_frames_received,
            payload.video_frames_decoded,
            payload.video_frames_presented,
            payload.video_frames_dropped,
            payload.video_key_frames_decoded,
            payload.video_codec,
            payload.decoder_implementation,
        )
        return {"accepted": True, "totalBytes": result.total_media_bytes}
    except ControlPlaneError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/{designator}/streams/requests/{request_id}/stop")
async def organization_stop_video_stream(
        request: Request, designator: str, request_id: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_streams", form_token)
    organization, user = await require_organization_user(
        request, designator, ("video_requester",)
    )
    try:
        result = await control_plane_store.stop_video_stream(
            request_id=request_id,
            organization_id=organization.id,
            requester_user_id=user.id,
        )
        await r2c_hub.send_video_stream_request_cancelled(
            device_credential_id=result.device_credential_id,
            request_id=result.id,
        )
        flash(request, "Video stream stopped.", "success")
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/streams",
        status_code=303,
    )


@app.post("/{designator}/settings")
async def organization_update_settings(
        request: Request,
        designator: str,
        records_visibility: Annotated[str, Form()],
        record_retention_days: Annotated[int, Form()],
        log_retention_days: Annotated[int, Form()],
        notification_email: Annotated[str, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner",),
    )
    try:
        await control_plane_store.update_settings(
            organization_id=organization.id,
            records_visibility=records_visibility,
            record_retention_days=record_retention_days,
            log_retention_days=log_retention_days,
            notification_email=notification_email,
            actor_id=user.id,
        )
        flash(request, "Organization settings updated.", "success")
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def deliver_organization_member_activation(
        request: Request,
        organization,
        member) -> bool:
    invitation = await control_plane_store.get_invitation(
        organization.designator,
        member.email,
    )
    if invitation is None:
        raise ControlPlaneError("Member activation invitation is unavailable.")
    activation_url = control_plane_tokens.activation_url(invitation)
    if CONTROL_PLANE_SIMULATION:
        request.session["_organization_invitation_url"] = activation_url
        return False
    await asyncio.to_thread(
        platform_admin_email_sender.send_organization_member_activation,
        recipient=member.email,
        member_name=member.display_name,
        organization_name=organization.legal_name,
        designator=organization.designator,
        activation_url=activation_url,
    )
    return True


@app.post("/{designator}/members")
async def organization_add_member(
        request: Request,
        designator: str,
        display_name: Annotated[str, Form()],
        email: Annotated[str, Form()],
        roles: Annotated[list[str], Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    try:
        member = await control_plane_store.add_user(
            organization_id=organization.id,
            display_name=display_name,
            email=email,
            roles=tuple(roles),
            actor_id=user.id,
        )
        activation_sent = await deliver_organization_member_activation(
            request,
            organization,
            member,
        )
        flash(
            request,
            (
                f"Added pending member {member.email}. "
                + (
                    "A seven-day activation invitation was emailed to them."
                    if activation_sent
                    else "They can activate using the generated invitation."
                )
            ),
            "success",
        )
    except (
        ControlPlaneError,
        InvalidOrganizationError,
        PlatformAdminAuthError,
        ValueError,
    ) as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin#members",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/members/{member_id}")
async def organization_update_member(
        request: Request,
        designator: str,
        member_id: str,
        display_name: Annotated[str, Form()],
        email: Annotated[str, Form()],
        form_token: Annotated[str, Form()],
        roles: Annotated[list[str] | None, Form()] = None):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    try:
        target = await control_plane_store.get_user(member_id)
        if (
            target is not None
            and target.organization_id == organization.id
            and "organization_owner" in target.roles
            and "organization_owner" not in user.roles
        ):
            raise ControlPlaneError(
                "Only the organization owner can edit the owner record."
            )
        member = await control_plane_store.update_user(
            organization_id=organization.id,
            user_id=member_id,
            display_name=display_name,
            email=email,
            roles=tuple(roles or ()),
            actor_id=user.id,
        )
        flash(request, f"Updated member {member.email}.", "success")
    except (ControlPlaneError, InvalidOrganizationError, ValueError) as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin#members",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/members/{member_id}/delete")
async def organization_delete_member(
        request: Request,
        designator: str,
        member_id: str,
        confirmation: Annotated[str, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    try:
        if confirmation != "delete":
            raise ControlPlaneError("Confirm that you want to delete this member.")
        target = await control_plane_store.get_user(member_id)
        if (
            target is not None
            and target.organization_id == organization.id
            and "organization_owner" in target.roles
            and "organization_owner" not in user.roles
        ):
            raise ControlPlaneError(
                "Only the organization owner can manage the owner record."
            )
        member = await control_plane_store.delete_user(
            organization_id=organization.id,
            user_id=member_id,
            actor_id=user.id,
        )
        flash(request, f"Deleted member {member.email} and revoked access.", "success")
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin#members",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/members/{member_id}/restore")
async def organization_restore_member(
        request: Request,
        designator: str,
        member_id: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    try:
        member = await control_plane_store.restore_user(
            organization_id=organization.id,
            user_id=member_id,
            actor_id=user.id,
        )
        activation_sent = await deliver_organization_member_activation(
            request,
            organization,
            member,
        )
        flash(
            request,
            (
                f"Restored {member.email} as a pending member. "
                + (
                    "A seven-day activation invitation was emailed to them."
                    if activation_sent
                    else "They can activate using the generated invitation."
                )
            ),
            "success",
        )
    except (
        ControlPlaneError,
        InvalidOrganizationError,
        PlatformAdminAuthError,
    ) as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin#members",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/members/{member_id}/invitation")
async def organization_send_member_invitation(
        request: Request,
        designator: str,
        member_id: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    try:
        member = await control_plane_store.renew_member_invitation(
            organization_id=organization.id,
            user_id=member_id,
            actor_id=user.id,
        )
        activation_sent = await deliver_organization_member_activation(
            request,
            organization,
            member,
        )
        if activation_sent:
            await control_plane_store.mark_member_invitation_sent(
                organization_id=organization.id,
                user_id=member.id,
                actor_id=user.id,
            )
        flash(
            request,
            (
                f"Sent a fresh seven-day activation invitation to {member.email}."
                if activation_sent
                else f"Generated a fresh activation invitation for {member.email}."
            ),
            "success",
        )
    except (
        ControlPlaneError,
        InvalidOrganizationError,
        PlatformAdminAuthError,
    ) as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin#members",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/enrollments")
async def organization_create_enrollment(
        request: Request,
        designator: str,
        label: Annotated[str, Form()],
        expires_in_hours: Annotated[int, Form()],
        max_redemptions: Annotated[int, Form()],
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    try:
        campaign = await control_plane_store.create_enrollment_campaign(
            organization_id=organization.id,
            label=label,
            created_by_user_id=user.id,
            expires_in_hours=expires_in_hours,
            max_redemptions=max_redemptions,
        )
        flash(request, f"Enrollment QR “{campaign.label}” created.", "success")
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/enrollments/{campaign_id}/revoke")
async def organization_revoke_enrollment(
        request: Request,
        designator: str,
        campaign_id: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    await control_plane_store.revoke_enrollment_campaign(
        campaign_id=campaign_id,
        organization_id=organization.id,
        actor_id=user.id,
    )
    flash(request, "Enrollment QR revoked.", "success")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/enrollments/{campaign_id}/renew")
async def organization_renew_enrollment(
        request: Request,
        designator: str,
        campaign_id: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    try:
        campaign = await control_plane_store.renew_enrollment_campaign(
            campaign_id=campaign_id,
            organization_id=organization.id,
            actor_id=user.id,
        )
        flash(
            request,
            f"Enrollment QR “{campaign.label}” renewed for seven days. Download the fresh QR before sharing it.",
            "success",
        )
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin#enrollment-qr",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/device-credentials/{credential_id}/extend")
async def organization_extend_device_credential(
        request: Request,
        designator: str,
        credential_id: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    try:
        credential = await control_plane_store.extend_device_credential(
            credential_id=credential_id,
            organization_id=organization.id,
            actor_id=user.id,
        )
        flash(
            request,
            f"Extended {credential.device_name} through {credential.expires_at.strftime('%d %b %Y')}.",
            "success",
        )
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin#device-authorizations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/device-credentials/{credential_id}/require-reauthentication")
async def organization_require_device_reauthentication(
        request: Request,
        designator: str,
        credential_id: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    try:
        credential = await control_plane_store.require_device_reauthentication(
            credential_id=credential_id,
            organization_id=organization.id,
            actor_id=user.id,
        )
        reauthentication_url = control_plane_tokens.device_reauthentication_url(
            credential_id=credential.id,
            organization_id=credential.organization_id,
            designator=organization.designator,
            requested_at=credential.reauth_requested_at.isoformat(),
        )
        await r2c_hub.disconnect_device_credential(
            credential.id,
            reason="Reauthentication required",
            reauthentication_url=reauthentication_url,
        )
        flash(
            request,
            (
                f"{credential.device_name} is blocked until it reauthenticates. "
                "Its managed RID map and credentials remain on the device while "
                "an authorized user signs in."
            ),
            "success",
        )
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin#device-authorizations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/{designator}/device-credentials/extend")
async def organization_extend_all_device_credentials(
        request: Request,
        designator: str,
        form_token: Annotated[str, Form()]):
    verify_csrf(request, "organization_admin", form_token)
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    try:
        credentials = await control_plane_store.extend_all_device_credentials(
            organization_id=organization.id,
            actor_id=user.id,
        )
        flash(
            request,
            f"Extended {len(credentials)} R2C tablet authorization{'s' if len(credentials) != 1 else ''} for at least one year.",
            "success",
        )
    except ControlPlaneError as exc:
        flash(request, str(exc), "warning")
    return RedirectResponse(
        url=f"/{organization.designator.lower()}/admin#device-authorizations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get(
    "/{designator}/enrollments/{campaign_id}/qr.svg",
    response_class=Response,
)
async def organization_enrollment_qr(
        request: Request,
        designator: str,
        campaign_id: str):
    organization, user = await require_organization_user(
        request,
        designator,
        ("organization_owner", "user_admin"),
    )
    campaign = await control_plane_store.get_enrollment_campaign(campaign_id)
    if (
        campaign is None
        or campaign.organization_id != organization.id
        or not campaign.is_usable()
    ):
        raise HTTPException(status_code=404, detail="Active enrollment not found.")
    import qrcode
    from qrcode.image.svg import SvgPathImage

    enrollment_url = control_plane_tokens.enrollment_url(
        organization,
        campaign,
    )
    app_enrollment_url = (
        "r2cenroll://open?url="
        + quote(enrollment_url, safe="")
    )
    image = qrcode.make(
        app_enrollment_url,
        image_factory=SvgPathImage,
        box_size=8,
        border=4,
    )
    return Response(
        content=image.to_string(),
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'inline; filename="{organization.designator}-'
                f'enrollment-{campaign.id}.svg"'
            ),
        },
    )


@app.get("/{designator}/enroll", response_class=HTMLResponse)
async def organization_enrollment_landing(
        request: Request,
        designator: str,
        token: str):
    if not organization_site_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Device enrollment is not configured.",
        )
    identity_name = await organization_page_identity(request, designator)
    enrollment_error = None
    organization = None
    campaign = None
    try:
        claims = control_plane_tokens.decode_enrollment(token)
        if claims.designator.lower() != designator.lower():
            raise EnrollmentTokenError("Device enrollment code is invalid.")
        organization = await control_plane_store.get_organization(designator)
        campaign = await control_plane_store.get_enrollment_campaign(
            claims.campaign_id
        )
        if (
            organization is None
            or campaign is None
            or claims.organization_id != organization.id
            or campaign.organization_id != organization.id
            or not secrets.compare_digest(
                claims.token_generation,
                campaign.token_generation,
            )
            or not campaign.is_usable()
        ):
            raise EnrollmentTokenError(
                "Device enrollment code is inactive or revoked."
            )
    except (
        ControlPlaneError,
        EnrollmentTokenError,
        InvalidOrganizationError,
    ) as exc:
        enrollment_error = str(exc)
    return templates.TemplateResponse(
        request=request,
        name="organization_enroll.html",
        context={
            "request": request,
            "enable_live_refresh": False,
            "include_leaflet": False,
            "include_datetime_script": False,
            "organization": organization,
            "campaign": campaign,
            "enrollment_error": enrollment_error,
            "simulation": CONTROL_PLANE_SIMULATION,
            "device_credential_issuance_enabled": (
                DEVICE_CREDENTIAL_ISSUANCE_ENABLED
            ),
            "public_configuration": (
                public_device_configuration(
                    organization,
                    tracker_base_url=(
                        CONTROL_PLANE_TRACKER_BASE_URL.rstrip("/")
                        + "/"
                        + organization.designator.lower()
                    ),
                    credential_issuance_enabled=(
                        DEVICE_CREDENTIAL_ISSUANCE_ENABLED
                    ),
                )
                if organization is not None
                else None
            ),
            "organization_page_designator": designator.upper(),
            "organization_identity_name": identity_name,
            "app_enrollment_url": (
                "r2cenroll://open?url="
                + quote(str(request.url), safe="")
            ),
        },
        status_code=(
            status.HTTP_400_BAD_REQUEST
            if enrollment_error
            else status.HTTP_200_OK
        ),
    )


@app.post("/api/v1/device-enrollment/redeem")
async def redeem_device_enrollment(payload: DeviceEnrollmentRedeemRequest):
    if (
        not organization_site_ready()
        or not DEVICE_CREDENTIAL_ISSUANCE_ENABLED
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Device credential issuance is not enabled.",
        )
    if (
        R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE > 0
        and payload.functionality_release < R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE
    ):
        raise HTTPException(
            status_code=status.HTTP_426_UPGRADE_REQUIRED,
            detail={
                "code": "upgrade_required",
                "minimum_functionality_release": R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE,
                "message": "Upgrade RID2Caltopo before enrolling this device.",
            },
            headers={"Upgrade": "RID2Caltopo"},
        )
    try:
        claims = control_plane_tokens.decode_enrollment(payload.token)
        organization = await control_plane_store.get_organization(
            claims.designator
        )
        campaign = await control_plane_store.get_enrollment_campaign(
            claims.campaign_id
        )
        if (
            organization is None
            or campaign is None
            or claims.organization_id != organization.id
            or campaign.organization_id != organization.id
            or not secrets.compare_digest(
                claims.token_generation,
                campaign.token_generation,
            )
        ):
            raise EnrollmentTokenError("Device enrollment code is invalid.")
        credential = await control_plane_store.issue_device_credential(
            campaign_id=campaign.id,
            organization_id=organization.id,
            device_name=payload.device_name,
            platform=payload.platform,
            functionality_release=payload.functionality_release,
        )
    except (
        ControlPlaneError,
        EnrollmentTokenError,
        InvalidOrganizationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return Response(
        content=json.dumps(
            {
                "schema_version": 1,
                "organization": {
                    "designator": organization.designator,
                    "name": organization.legal_name,
                },
                "tracker": {
                    "base_url": (
                        CONTROL_PLANE_TRACKER_BASE_URL.rstrip("/")
                        + "/"
                        + organization.designator.lower()
                    ),
                    "api_key": credential.token,
                    "faa_proxy_url": (
                        CONTROL_PLANE_TRACKER_BASE_URL + "/faa/notams"
                    ),
                },
                "credential": {
                    "id": credential.id,
                    "expires_at": credential.expires_at.isoformat(),
                    "revocable": True,
                },
            }
        ),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


# List the admin page
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
        request: Request):
    """Send the retired global admin entry point to platform administration."""
    next_url = "/platform-admin/organizations"
    return RedirectResponse(
        url=f"/platform-admin/login?{urlencode({'next': next_url})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )

@app.post("/admin/edit/{flight_id}")
async def edit_flight(
        request: Request,
        flight_id: int,
        new_sar_id: Annotated[str, Form()],
        new_uas: Annotated[str, Form()],
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin)):

    result = await db.execute(
        select(Flight).filter(
            Flight.id == flight_id,
            Flight.organization_id.is_(None),
        )
    )
    flight = result.scalar_one_or_none()
    if not flight:
        return {"error": f"Flight {flight_id} undefined"}

    new_sar_id = new_sar_id.upper().strip()
    new_uas = new_uas.lower().strip()
    result = await find_overlap(
        db,
        flight.start_time,
        flight.end_time,
        remote_id=flight.remote_id,
        sar_id=new_sar_id,
    )
    overlap = result.scalars().first()

    if overlap and {flight_id} != {overlap.id}:
        flash(request, f"Flight {flight_id} edit rejected. Change would overlap w/flight record {overlap.id}", "warning")
    elif overlap and overlap.sar_id == new_sar_id and overlap.uas == new_uas:
        flash(request, f"No change detected for flight {overlap.id}", "info")
    else:
        flight.sar_id = new_sar_id
        flight.uas = new_uas
        await db.commit()
        flash(request, f"Flight {flight_id} successfully edited", "success")
    return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)

# delete a single flight:
@app.post("/admin/delete/{flight_id}")  # Must be .post
async def delete_flight(
        request: Request,
        flight_id: int,
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin)):

    result = await db.execute(
        select(Flight).filter(
            Flight.id == flight_id,
            Flight.organization_id.is_(None),
        )
    )
    flight = result.scalar_one_or_none()
    if not flight:
        flash(request, f"Flight {flight_id} not found", "warning")
        return RedirectResponse(url=admin_url(start_date, end_date, error="not_found"), status_code=status.HTTP_303_SEE_OTHER)

    await db.delete(flight)
    await db.commit()
    flash(request, f"Flight {flight_id} deleted successfully", "success")
    return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/batch")
async def batch_update_flights(
        request: Request,
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin)):

    form_data = await request.form()
    action, flight_ids, delete_ids, updates = parse_admin_batch_form(form_data)

    if not flight_ids:
        flash(request, "No flights were submitted.", "info")
        return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)

    result = await db.execute(
        select(Flight).where(
            Flight.id.in_(flight_ids),
            Flight.organization_id.is_(None),
        )
    )
    flights = {flight.id: flight for flight in result.scalars().all()}
    missing_ids = [flight_id for flight_id in flight_ids if flight_id not in flights]

    if action == "delete_selected":
        if not delete_ids:
            flash(request, "No flights were selected for deletion.", "info")
            return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)

        deleted_ids = []
        for flight_id in flight_ids:
            if flight_id not in delete_ids:
                continue
            flight = flights.get(flight_id)
            if not flight:
                continue
            await db.delete(flight)
            deleted_ids.append(flight_id)

        if deleted_ids:
            await db.commit()
            flash(request, f"Deleted {len(deleted_ids)} flight(s): {', '.join(str(flight_id) for flight_id in deleted_ids)}", "success")
        else:
            flash(request, "Selected flights were not found.", "warning")

        if missing_ids:
            flash(request, f"Skipped missing flight(s): {', '.join(str(flight_id) for flight_id in missing_ids)}", "warning")

        return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)

    changed_ids = []
    rejected_changes = []

    for flight_id in flight_ids:
        flight = flights.get(flight_id)
        if not flight:
            continue

        submitted = updates[flight_id]
        new_sar_id = submitted["sar_id"]
        new_uas = submitted["uas"]

        if flight.sar_id == new_sar_id and flight.uas == new_uas:
            continue

        result = await find_overlap(
            db,
            flight.start_time,
            flight.end_time,
            remote_id=flight.remote_id,
            sar_id=new_sar_id,
        )
        overlap = result.scalars().first()

        if overlap and overlap.id != flight.id:
            rejected_changes.append((flight_id, overlap.id))
            continue

        flight.sar_id = new_sar_id
        flight.uas = new_uas
        changed_ids.append(flight_id)

    if changed_ids:
        await db.commit()
        flash(request, f"Saved changes for {len(changed_ids)} flight(s): {', '.join(str(flight_id) for flight_id in changed_ids)}", "success")
    else:
        flash(request, "No field changes were detected.", "info")

    for flight_id, overlap_id in rejected_changes:
        flash(request, f"Flight {flight_id} edit rejected. Change would overlap w/flight record {overlap_id}", "warning")

    if missing_ids:
        flash(request, f"Skipped missing flight(s): {', '.join(str(flight_id) for flight_id in missing_ids)}", "warning")

    return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)

# delete entire database:
@app.post("/admin/delete")  # Must be .post
async def reset_table(
        request: Request,
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        user: str = Depends(check_admin),
        db: AsyncSession = Depends(get_db)):
    
    await db.execute(delete(Flight).where(Flight.organization_id.is_(None)))
    await db.commit()
    flash(request, f"flights table successfully cleaned.", "success")
    return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)

# export timestamped .csv representation of the database:
@app.get("/export", response_class=Response, responses={
    200: {
        "content": {"text/csv": {}},
        "description": "Return a CSV file of all flight logs.",
    }
})
async def export(
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        admin_user: bool = Depends(opt_check_admin),
        db: AsyncSession = Depends(get_db)):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Base query:
    stmt = apply_date_filter(
        select(Flight).where(Flight.organization_id.is_(None)),
        start_date,
        end_date,
    )

    stmt = stmt.order_by(Flight.start_time)

    result = await db.execute(stmt)
    flights = result.scalars().all()

    
    output = io.StringIO()
    writer = csv.writer(output)
    # N.B. keys need to match those used in import:
    if admin_user:
        filename = "r2c_audit_full"
        writer.writerow(["Flight", "Sar Id", "Remote Id", "UAS", "Incident", "Op Period", "Map Id", "Start Time", "End Time",
                         "Start Lattitude", "Start Longitude", "Hours", "Distance (mi)",
                         "Temp (F)", "Rel Humidity (%)", "Dew Pt (F)", "Precip (in)", "Wind (mph)", "Gusts (mph)",
                         "Cloud Cover (%)", "Time Of Day", "Archive Path"])
    else:
        filename = "r2c_audit_part"
        writer.writerow(["Flight", "Sar Id", "Remote Id", "UAS", "Start Time", "End Time", "Hours", "Distance (mi)",
                         "Temp (F)", "Rel Humidity (%)", "Dew Pt (F)", "Precip (in)", "Wind (mph)", "Gusts (mph)",
                         "Cloud Cover (%)", "Time Of Day"])
        
    
    for f in flights:
        if admin_user:
            writer.writerow([f.id, f.sar_id.upper(), (f.remote_id or "").upper(), f.uas.lower(), f.incident, f.op_period, f.map_id.upper(),
                             format_datetime(f.start_time.replace(tzinfo=UTC)),
                             format_datetime(f.end_time.replace(tzinfo=UTC)),
                             f.start_lat, f.start_lng, f.hours, f.distance_mi, f.temp_f,
                             f.rhum_pct, f.dewpt_f, f.precip_in, f.wind_mph, f.gusts_mph,
                             f.cloudcvr_pct, f.timeofday, f.archive_relpath or ""])
        else:
            writer.writerow([f.id, f.sar_id.upper(), (f.remote_id or "").upper(), f.uas.lower(), 
                             format_datetime(f.start_time.replace(tzinfo=UTC)),
                             format_datetime(f.end_time.replace(tzinfo=UTC)),
                             f.hours, f.distance_mi, f.temp_f,
                             f.rhum_pct, f.dewpt_f, f.precip_in, f.wind_mph, f.gusts_mph,
                             f.cloudcvr_pct, f.timeofday])
            
    csv_content = output.getvalue()

    return Response(
        content=csv_content, 
        media_type="text/csv", 
        headers={"Content-Disposition": f"attachment; filename={filename}_{timestamp}.csv"}
    )

# Append new flights to the database.
# Pair with /export and /admin/delete for archive/restore functionality:
@app.post("/admin/import")
async def import_csv(
        file: UploadFile = File(...),
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin)):
    content = await file.read()
    decoded = content.decode('utf-8')
    input_file = io.StringIO(decoded)
    reader = csv.DictReader(input_file)
    retval = {"error": "unspecified"}
    admin_archive = False
    try:
        for row in reader:
            if not admin_archive:
                if not row.get('Start Lattitude'):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Archive wasn't produced with admin privileges."
                    )
                else:
                    admin_archive = True
                
            # N.B. keys need to match those used in export.
            # Create a new Flight object for each row
            new_flight = Flight(
                sar_id=row.get('Sar Id', '').upper(),
                remote_id=normalize_remote_id(row.get('Remote Id', '')),
                uas=row.get('UAS', '').lower(),
                incident=row.get('Incident', ''),
                op_period=row.get('Op Period', ''),
                map_id=row.get('Map Id', ''),
                start_time=datetime_from_format(row.get('Start Time', None)),
                end_time=datetime_from_format(row.get('End Time', None)),
                start_lat=float(row.get('Start Lattitude', 0.0)),
                start_lng=float(row.get('Start Longitude', 0.0)),
                hours=float(row.get('Hours', 0.0)),
                distance_mi=float(row.get('Distance (mi)', 0.0)),
                temp_f=float(row.get('Temp (F)', 0.0)),
                rhum_pct=float(row.get('Rel Humidity (%)', 0.0)),
                dewpt_f=float(row.get('Dew Pt (F)', 0.0)),
                precip_in=float(row.get('Precip (in)', 0.0)),
                wind_mph=float(row.get('Wind (mph)', 0.0)),
                gusts_mph=float(row.get('Gusts (mph)', 0.0)),
                cloudcvr_pct=float(row.get('Cloud Cover (%)', 0.0)),
                timeofday=row.get('Time Of Day', ""),
                archive_relpath=row.get('Archive Path', ""),
            )
            db.add(new_flight)
        
        await db.commit()
        return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        db.rollback()
        retval = {"error": f"Import failed: {str(e)}"}

    return retval


@app.post("/admin/backfill-csv")
async def backfill_csv(
        request: Request,
        file: UploadFile = File(...),
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin)):
    content = await file.read()
    decoded = content.decode('utf-8')
    input_file = io.StringIO(decoded)
    reader = csv.DictReader(input_file)

    rows = list(reader)
    if not rows:
        flash(request, "CSV backfill file was empty.", "warning")
        return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)

    if not rows[0].get('Start Lattitude'):
        flash(request, "Backfill requires the full admin CSV export.", "warning")
        return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)

    result = await db.execute(
        select(Flight).where(Flight.organization_id.is_(None))
    )
    flights = result.scalars().all()

    unmatched_flights = []
    used_flight_ids = set()
    flight_lookup = {}
    for flight in flights:
        key = (
            normalize_csv_value(flight.sar_id).upper(),
            normalize_remote_id(flight.remote_id),
            normalize_csv_value(flight.uas).lower(),
            normalize_match_datetime(flight.start_time),
            normalize_match_datetime(flight.end_time),
        )
        flight_lookup.setdefault(key, []).append(flight)
        unmatched_flights.append(flight)

    updated_count = 0
    missing_count = 0

    for row in rows:
        start_time = datetime_from_format(row.get('Start Time', None))
        end_time = datetime_from_format(row.get('End Time', None))
        key = (
            normalize_csv_value(row.get('Sar Id', '')).upper(),
            normalize_remote_id(row.get('Remote Id', '')),
            normalize_csv_value(row.get('UAS', '')).lower(),
            normalize_match_datetime(start_time),
            normalize_match_datetime(end_time),
        )
        matches = flight_lookup.get(key, [])
        while matches and matches[0].id in used_flight_ids:
            matches.pop(0)
        flight = matches.pop(0) if matches else None

        if flight is None:
            csv_start_lat = parse_csv_float(row.get('Start Lattitude', 0.0))
            csv_start_lng = parse_csv_float(row.get('Start Longitude', 0.0))
            fallback_candidates = [
                candidate for candidate in unmatched_flights
                if datetime_match_within_seconds(candidate.start_time, start_time)
                and datetime_match_within_seconds(candidate.end_time, end_time)
                and coordinates_match(candidate.start_lat, candidate.start_lng, csv_start_lat, csv_start_lng)
            ]
            if len(fallback_candidates) == 1:
                flight = fallback_candidates[0]
            else:
                missing_count += 1
                continue

        used_flight_ids.add(flight.id)
        if flight in unmatched_flights:
            unmatched_flights.remove(flight)

        flight.incident = normalize_csv_value(row.get('Incident', ''))
        flight.op_period = normalize_csv_value(row.get('Op Period', ''))
        remote_id = normalize_remote_id(row.get('Remote Id', ''))
        if remote_id:
            flight.remote_id = remote_id
        flight.map_id = normalize_csv_value(row.get('Map Id', '')).upper()
        flight.start_lat = parse_csv_float(row.get('Start Lattitude', 0.0))
        flight.start_lng = parse_csv_float(row.get('Start Longitude', 0.0))
        flight.hours = parse_csv_float(row.get('Hours', 0.0))
        flight.distance_mi = parse_csv_float(row.get('Distance (mi)', 0.0))
        flight.temp_f = parse_csv_float(row.get('Temp (F)', 0.0))
        flight.rhum_pct = parse_csv_float(row.get('Rel Humidity (%)', 0.0))
        flight.dewpt_f = parse_csv_float(row.get('Dew Pt (F)', 0.0))
        flight.precip_in = parse_csv_float(row.get('Precip (in)', 0.0))
        flight.wind_mph = parse_csv_float(row.get('Wind (mph)', 0.0))
        flight.gusts_mph = parse_csv_float(row.get('Gusts (mph)', 0.0))
        flight.cloudcvr_pct = parse_csv_float(row.get('Cloud Cover (%)', 0.0))
        flight.timeofday = normalize_csv_value(row.get('Time Of Day', ''), "day")
        archive_path = normalize_csv_value(row.get('Archive Path', ''))
        if archive_path and not flight.archive_relpath:
            flight.archive_relpath = archive_path
        updated_count += 1

    await db.commit()

    flash(request, f"Backfilled {updated_count} flight(s) from CSV.", "success")
    if missing_count:
        flash(request, f"Could not match {missing_count} CSV row(s) to rebuilt flights.", "warning")

    return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/import-archive")
async def import_flight_archive(
        request: Request,
        file: UploadFile = File(...),
        start_date: Annotated[Optional[date], Form()] = None,
        end_date: Annotated[Optional[date], Form()] = None,
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin)):
    imported_count = 0
    skipped_files = []
    batch_written_paths = []

    try:
        existing_count = await db.scalar(select(func.count()).select_from(Flight))
        if existing_count:
            flash(request, "Archive import requires an empty flights table to avoid duplicates.", "warning")
            return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)

        content = await read_upload_with_limit(file)
        archive_bytes = io.BytesIO(content)
        with tarfile.open(fileobj=archive_bytes, mode="r:*") as tar:
            members = reviewed_flight_archive_members(tar)

            for member in members:
                extracted = tar.extractfile(member)
                if extracted is None:
                    skipped_files.append(f"{member.name}: unreadable")
                    continue

                try:
                    data = json.load(extracted)
                    flight_inputs = await extract_flight_inputs_from_geojson(data)
                    _, archive_path = await create_imported_flight_and_archive(db, data, flight_inputs)
                    batch_written_paths.append(archive_path)
                    imported_count += 1
                    if imported_count % 50 == 0:
                        await db.commit()
                        batch_written_paths.clear()
                except Exception as exc:
                    await db.rollback()
                    for archive_path in batch_written_paths:
                        try:
                            if os.path.exists(archive_path):
                                os.unlink(archive_path)
                        except OSError:
                            pass
                    batch_written_paths.clear()
                    skipped_files.append(f"{member.name}: {exc}")

            if imported_count % 50 != 0:
                await db.commit()
                batch_written_paths.clear()

        if imported_count:
            flash(request, f"Imported {imported_count} flight log(s) from archive.", "success")
        else:
            flash(request, "No flight logs were imported from the archive.", "warning")

        if skipped_files:
            flash(request, f"Skipped {len(skipped_files)} file(s). First issue: {skipped_files[0]}", "warning")

        return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)
    except Exception as exc:
        await db.rollback()
        for archive_path in batch_written_paths:
            try:
                if os.path.exists(archive_path):
                    os.unlink(archive_path)
            except OSError:
                pass
        flash(request, f"Archive import failed: {exc}", "warning")
        return RedirectResponse(url=admin_url(start_date, end_date), status_code=status.HTTP_303_SEE_OTHER)

@app.get("/flightlogs/list", response_class=HTMLResponse)
async def list_flight_logs(
    request: Request,
    year: Optional[str] = None,
    month: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(check_admin)):
    """
    Lists all archived flight logs, optionally filtered by year and month,
    organized by year and month.
    """
    all_logs = {}
    if not os.path.exists(BASE_LOG_DIRECTORY):
        return {"message": "No flight logs directory found.", "logs": {}}

    # Determine the base path for listing based on provided filters
    search_path = BASE_LOG_DIRECTORY
    if year:
        search_path = os.path.join(search_path, year)
        if month:
            search_path = os.path.join(search_path, month)
    elif month:
        search_path = os.path.join(search_path, datetime.now().strftime("%Y"))
        search_path = os.path.join(search_path, month)

    flight_result = await db.execute(
        select(Flight.id, Flight.archive_relpath).where(
            Flight.organization_id.is_(None)
        )
    )
    archive_lookup = {
        (archive_relpath or ""): flight_id
        for flight_id, archive_relpath in flight_result.all()
        if archive_relpath
    }

    # Walk through the directories to find logs
    for root, dirs, files in os.walk(search_path):
        # Extract year and month from the path relative to BASE_LOG_DIRECTORY
        relative_path = os.path.relpath(root, BASE_LOG_DIRECTORY)
        path_parts = relative_path.split(os.sep)

        current_year = None
        current_month = None

        if len(path_parts) >= 1 and path_parts[0].isdigit() and len(path_parts[0]) == 4:
            current_year = path_parts[0]
        if len(path_parts) >= 2 and path_parts[1].isdigit() and len(path_parts[1]) == 2:
            current_month = path_parts[1]

        if current_year and current_month:
            if current_year not in all_logs:
                all_logs[current_year] = {'total_flights': 0, 'months': {}}
            if current_month not in all_logs[current_year]['months']:
                all_logs[current_year]['months'][current_month] = {'total_flights': 0, 'flights': []}

            for filename in files:
                if filename.endswith(".json") and filename.startswith("flightlog_"):
                    timestamp_and_title = filename[len("flightlog_"):-len(".json")]
                    timestamp_part, title_part = (timestamp_and_title.split("-", 1) + [""])[:2]
                    relpath = os.path.join(current_year, current_month, filename)
                    flight_id = archive_lookup.get(relpath)
                    display_filename = filename
                    if flight_id:
                        display_filename = re.sub(rf"^flightlog_{flight_id}_", "flightlog_", filename, count=1)
                    flight_dt = datetime.min
                    try:
                        # Parse the full timestamp when it is present and well-formed.
                        flight_dt = datetime.strptime(timestamp_part, "%d%b%Y_%H%M%S_%Z")
                    except ValueError:
                        try:
                            # Fall back to the date/time portion if the timezone suffix is irregular.
                            timestamp_without_tz = timestamp_part.rsplit("_", 1)[0]
                            flight_dt = datetime.strptime(timestamp_without_tz, "%d%b%Y_%H%M%S")
                        except ValueError:
                            pass

                    all_logs[current_year]['months'][current_month]['flights'].append({
                        "flight_id": flight_id,
                        "filename": filename,
                        "display_filename": display_filename,
                        "timestamp_str": timestamp_part if flight_dt != datetime.min else "N/A",
                        "timestamp_dt": flight_dt,
                        "title": title_part,
                        "download_url": f"/flightlogs/download/{current_year}/{current_month}/{filename}"
                    })
                    all_logs[current_year]['total_flights'] += 1
                    all_logs[current_year]['months'][current_month]['total_flights'] += 1

    # Sort the results: years (newest to oldest), months (newest to oldest), flights (newest to oldest)
    sorted_logs = {}
    for year_key in sorted(all_logs.keys(), reverse=True):
        sorted_logs[year_key] = all_logs[year_key]
        sorted_logs[year_key]['months'] = dict(sorted(
            all_logs[year_key]['months'].items(), key=lambda item: item[0], reverse=True
        ))
        for month_key in sorted_logs[year_key]['months']:
            sorted_logs[year_key]['months'][month_key]['flights'].sort(
                key=lambda x: (x['timestamp_dt'], x['filename']), reverse=True
            )
    return templates.TemplateResponse(
        request=request,
        name="flightlogs.html",
        context={
            "request": request,
            "logs_data": sorted_logs,
            "current_year" : datetime.now().strftime("%Y"),
            "selected_year" : year,
            "selected_month" : month
        },
    )


@app.get("/flightlogs/download/{year}/{month}/{filename}", response_class=FileResponse, responses={
    200: {
        "content": {"application/geo+json": {}},
        "description": "Return geo-json flight log.",
    }
})
async def download_flight_log(
        year: str,
        month: str,
        filename: str,
        admin_user: bool = Depends(check_admin) ):
    """
    Downloads a specific geo-json flight log file.
    """
    filepath = os.path.join(BASE_LOG_DIRECTORY, year, month, filename)
    if not os.path.exists(filepath) or not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Flight log not found.")

    return FileResponse(filepath, media_type="application/geo+json", filename=filename)

@app.get("/flightlogs/archive", response_class=FileResponse, responses={
    200: {
        "content": {"application/gzip": {}},
        "description": "Return compressed archive of flight logs.",
    }
})
async def download_all_flight_logs_archive(
        bg_tasks: BackgroundTasks,
        admin_user: bool = Depends(check_admin) ):
    """
    Creates and downloads a timestamped .tgz archive of all flight logs.
    """
    if not os.path.exists(BASE_LOG_DIRECTORY) or not os.listdir(BASE_LOG_DIRECTORY):
        raise HTTPException(status_code=404, detail="No flight logs to archive.")

    archive_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_filename = f"r2c-tracker-flightlogs-archive_{archive_timestamp}.tgz"
    tmp_dir = os.path.join(BASE_LOG_DIRECTORY, "tmp")

    try:
        os.makedirs(tmp_dir, exist_ok=True)
        temp_archive_path = os.path.join(tmp_dir, archive_filename)
        with tarfile.open(temp_archive_path, "w:gz") as tar:
            legacy_years = [
                name
                for name in os.listdir(BASE_LOG_DIRECTORY)
                if re.fullmatch(r"\d{4}", name)
                and os.path.isdir(os.path.join(BASE_LOG_DIRECTORY, name))
            ]
            if not legacy_years:
                raise HTTPException(
                    status_code=404,
                    detail="No legacy flight logs to archive.",
                )
            for year_name in sorted(legacy_years):
                tar.add(
                    os.path.join(BASE_LOG_DIRECTORY, year_name),
                    arcname=year_name,
                )
        bg_tasks.add_task(os.unlink, temp_archive_path)
        return FileResponse(temp_archive_path, media_type="application/gzip", filename=archive_filename)

    except Exception as e:
        print(f"Error creating archive: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create archive: {e}")

@app.get("/flightlogs/archive/current-year", response_class=FileResponse, responses={
    200: {
        "content": {"application/gzip": {}},
        "description": "Return compressed archive of flight logs.",
    }
})
async def download_current_year_flight_logs_archive(
        bg_tasks: BackgroundTasks,
        admin_user: bool = Depends(check_admin) ):
    """
    Creates and downloads a timestamped .tgz archive of current year's flight logs.
    """
    current_year = datetime.now().strftime("%Y")
    year_log_path = os.path.join(BASE_LOG_DIRECTORY, current_year)

    if not os.path.exists(year_log_path) or not os.listdir(year_log_path):
        raise HTTPException(status_code=404, detail=f"No flight logs found for year {current_year}.")

    archive_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_filename = f"r2c-tracker-flightlogs-{current_year}-archive_{archive_timestamp}.tgz"
    tmp_dir = os.path.join(BASE_LOG_DIRECTORY, "tmp")

    try:
        os.makedirs(tmp_dir, exist_ok=True)
        temp_archive_path = os.path.join(tmp_dir, archive_filename)
        with tarfile.open(temp_archive_path, "w:gz") as tar:
            tar.add(year_log_path, arcname=os.path.basename(year_log_path))
        bg_tasks.add_task(os.unlink, temp_archive_path)
        return FileResponse(temp_archive_path, media_type="application/gzip", filename=archive_filename)

    except Exception as e:
        print(f"Error creating current year archive: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create current year archive: {e}")

    
        
@app.get("/{designator}/streams/live-status")
async def organization_stream_live_status(
        request: Request,
        designator: str,
        device: str = "",
        stream: str = ""):
    """Return the small, authenticated model used for in-place previews."""
    organization, _user = await require_organization_user(
        request,
        designator,
        ("video_requester",),
    )
    streams, organization_requests = await asyncio.gather(
        control_plane_store.list_active_video_streams(organization.id),
        control_plane_store.list_video_stream_requests(
            organization_id=organization.id,
        ),
    )
    clean_device_id = device.strip()
    clean_stream = stream.strip().lower()
    if clean_device_id:
        streams = tuple(
            item for item in streams
            if item.device_credential_id == clean_device_id
        )
        organization_requests = tuple(
            item for item in organization_requests
            if item.device_credential_id == clean_device_id
        )
    if clean_stream:
        streams = tuple(
            item for item in streams
            if item.drone_designator.strip().lower() == clean_stream
        )
    values = []
    for item in streams:
        revision = item.thumbnail_revision.strip()
        thumbnail_url = ""
        if revision:
            thumbnail_url = (
                "/r2c-thumbnail/"
                f"{tablet_link_code(organization.designator, item.device_name)}/"
                f"{quote(item.session_id, safe='')}.jpg?"
                + urlencode({"rev": revision})
            )
        source_label = "Source details pending"
        if item.source_width and item.source_height:
            source_label = f"{item.source_width}×{item.source_height}"
            if item.source_fps:
                source_label += f" at {item.source_fps:.1f} fps"
        values.append({
            "sessionId": item.session_id,
            "thumbnailRevision": revision,
            "thumbnailUrl": thumbnail_url,
            "sourceLabel": source_label,
        })
    return JSONResponse(
        {
            "membershipRevision": stream_membership_revision(streams),
            "inProgressSessionIds": sorted({
                item.stream_session_id
                for item in organization_requests
                if item.state in {
                    "pending",
                    "probing",
                    "awaiting_approval",
                    "approved",
                    "streaming",
                }
                and item.expires_at >= datetime.now(UTC)
            }),
            "streams": values,
        },
        headers={
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.websocket("/{designator}/streams/events")
async def organization_stream_events(
        websocket: WebSocket,
        designator: str,
        device: str = "",
        stream: str = ""):
    """Notify an authenticated viewer only when stream lifecycle state changes."""
    if not organization_site_ready():
        await websocket.close(code=1013, reason="Organization site unavailable")
        return
    organization = await control_plane_store.get_organization(designator)
    user_id = websocket.session.get("organization_user_id")
    session_designator = websocket.session.get("organization_designator")
    user = await control_plane_store.get_user(user_id) if user_id else None
    if (
        organization is None
        or user is None
        or user.state != "active"
        or user.organization_id != organization.id
        or session_designator != organization.designator
        or "video_requester" not in user.roles
    ):
        await websocket.close(code=1008, reason="Organization login required")
        return

    clean_device_id = device.strip()
    clean_stream = stream.strip().lower()

    async def renew_thumbnail_preview() -> None:
        active_streams = await control_plane_store.list_active_video_streams(
            organization.id
        )
        for device_id in thumbnail_preview_device_ids(
            active_streams,
            clean_device_id,
        ):
            delivered = await r2c_hub.send_video_thumbnail_preview(
                device_credential_id=device_id,
                ttl_seconds=25,
            )
            if not delivered:
                await control_plane_store.notify_video_thumbnail_preview(
                    organization_id=organization.id,
                    device_credential_id=device_id,
                    ttl_seconds=25,
                )

    async def current_status():
        streams, requests = await asyncio.gather(
            control_plane_store.list_active_video_streams(organization.id),
            control_plane_store.list_video_stream_requests(
                organization_id=organization.id,
                requester_user_id=user.id,
            ),
        )
        if clean_device_id:
            streams = tuple(
                item for item in streams
                if item.device_credential_id == clean_device_id
            )
        if clean_stream:
            streams = tuple(
                item for item in streams
                if item.drone_designator.strip().lower() == clean_stream
            )
        return organization_stream_status(streams, requests)

    await organization_stream_event_hub.connect(
        organization.id,
        organization.designator,
        user.email,
        websocket,
    )
    try:
        status_snapshot = await current_status()
        await websocket.send_json({
            "type": "ready",
            "active": status_snapshot["active"],
            "revision": status_snapshot["revision"],
            "membershipRevision": status_snapshot["membership_revision"],
        })
        while True:
            await renew_thumbnail_preview()
            expiry = status_snapshot["next_expiry"]
            # PostgreSQL notifications are the fast path.  Reconcile over the
            # already-open focused-page socket as a bounded fallback so a
            # missed notification or a Cloud Run revision handoff repairs
            # itself without another connection or operator refresh.
            # Approval can race with the reconnect which follows preflight.
            # Reconcile quickly while the requester is specifically awaiting
            # the pilot decision so a missed cross-instance notification does
            # not add a fixed 30-second delay before media signaling.
            timeout_seconds = (
                1.0 if status_snapshot["awaiting_approval"] else 30.0
            )
            # A live catalog can represent one tablet or several.  Renew its
            # 25-second device leases before they expire in either case.
            if status_snapshot["active"]:
                timeout_seconds = min(timeout_seconds, 10.0)
            if expiry is not None:
                expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)
                timeout_seconds = min(
                    timeout_seconds,
                    max(
                        0.25,
                        (expiry.astimezone(UTC) - datetime.now(UTC)).total_seconds(),
                    ),
                )
            try:
                client_message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=timeout_seconds,
                )
                if client_message == "unsubscribe":
                    await websocket.send_json({"type": "unsubscribed"})
                    return
            except asyncio.TimeoutError:
                current = await current_status()
                if current["revision"] != status_snapshot["revision"]:
                    await websocket.send_json({"type": "streams_changed"})
                    return
                status_snapshot = current
    except WebSocketDisconnect:
        pass
    finally:
        await organization_stream_event_hub.disconnect(
            organization.id, websocket
        )


async def serve_r2c_websocket(
        websocket: WebSocket,
        organization_designator: str):
    token = websocket.headers.get(API_KEY_NAME)
    authenticated, device_credential = await authenticate_tracker_session(token)
    normalized_token = _normalize_tracker_token(token)
    reauthentication_challenge = (
        await control_plane_store.device_reauthentication_challenge(normalized_token)
        if not authenticated and control_plane_store is not None
        else None
    )
    if reauthentication_challenge is not None:
        credential_record, designator = reauthentication_challenge
        reauthentication_url = control_plane_tokens.device_reauthentication_url(
            credential_id=credential_record.id,
            organization_id=credential_record.organization_id,
            designator=designator,
            requested_at=credential_record.reauth_requested_at.isoformat(),
        )
        await websocket.accept()
        await websocket.send_json({
            "type": "reauthentication_required",
            "clearManagedConfiguration": False,
            "reauthenticationUrl": reauthentication_url,
            "message": (
                "This device must reauthenticate before Tracker access can be restored."
            ),
        })
        await websocket.close(code=1008, reason="Reauthentication required")
        return
    organization_mismatch = (
        device_credential is None
        or device_credential.designator.lower()
        != organization_designator.strip().lower()
    )
    if not authenticated or organization_mismatch:
        client_host = websocket.client.host if websocket.client else "unknown"
        logger.warning(
            "r2c websocket auth rejected: client=%s organization=%s "
            "user_agent=%s %s",
            client_host,
            organization_designator,
            websocket.headers.get("user-agent", ""),
            _describe_tracker_token_mismatch(token, ""),
        )
        await websocket.close(
            code=1008,
            reason=(
                "Organization credential mismatch"
                if organization_mismatch
                else "Invalid tracker token"
            ),
        )
        return
    client_host = websocket.client.host if websocket.client else "unknown"
    await r2c_hub.connect(websocket, device_credential)
    logger.debug(
        "r2c websocket connected: client=%s user_agent=%s",
        client_host,
        websocket.headers.get("user-agent", ""),
    )
    try:
        while True:
            payload = json.loads(await websocket.receive_text())
            if isinstance(payload, dict):
                if payload.get("type") == "hello":
                    functionality_release = R2CCoordinationHub._parse_nonnegative_int(
                        payload.get("trackerFunctionalityRelease")
                    )
                    if (
                        R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE > 0
                        and functionality_release < R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE
                    ):
                        await websocket.send_json({
                            "type": "upgrade_required",
                            "minimumFunctionalityRelease": (
                                R2C_MIN_TRACKER_FUNCTIONALITY_RELEASE
                            ),
                            "message": "Upgrade RID2Caltopo to restore Tracker access.",
                        })
                        await websocket.close(code=1008, reason="Upgrade required")
                        return
                await r2c_hub.handle_message(websocket, payload)
    except WebSocketDisconnect as e:
        conn_info = await r2c_hub.get_connection_debug_info(websocket)
        close_code = getattr(e, "code", "")
        close_reason = getattr(e, "reason", "")
        close_logger = logger.debug if (
            close_code == 1000
            and close_reason == "client-stop"
            and int(conn_info.get("conn_age_ms", 0) or 0) < 5000
        ) else logger.info
        close_logger(
            "r2c websocket closed: client=%s code=%s reason=%s map=%s zone=%s guid=%s conn_age_ms=%s hello_age_ms=%s last_seen_age_ms=%s",
            client_host,
            close_code,
            close_reason,
            conn_info.get("map_id", ""),
            conn_info.get("zone_id", ""),
            conn_info.get("guid", ""),
            conn_info.get("conn_age_ms", ""),
            conn_info.get("hello_age_ms", ""),
            conn_info.get("last_seen_age_ms", ""),
        )
        await r2c_hub.disconnect(websocket)
    except Exception as e:
        conn_info = await r2c_hub.get_connection_debug_info(websocket)
        logger.warning(
            "r2c websocket error: client=%s error=%s map=%s zone=%s guid=%s conn_age_ms=%s hello_age_ms=%s last_seen_age_ms=%s",
            client_host,
            e,
            conn_info.get("map_id", ""),
            conn_info.get("zone_id", ""),
            conn_info.get("guid", ""),
            conn_info.get("conn_age_ms", ""),
            conn_info.get("hello_age_ms", ""),
            conn_info.get("last_seen_age_ms", ""),
        )
        await r2c_hub.disconnect(websocket)


@app.websocket("/{designator}/ws/r2c")
async def organization_r2c_websocket_endpoint(
        websocket: WebSocket,
        designator: str):
    """Serve organization-bound R2C clients on the managed tracker path."""
    await serve_r2c_websocket(websocket, designator)


@app.get("/t/{tablet_code}")
async def connected_tablet_short_link(
        tablet_code: str):
    """Resolve an ephemeral tablet alias to its authenticated portal path."""
    tablet = await r2c_hub.resolve_tablet_link_code(tablet_code)
    if tablet is None:
        raise HTTPException(status_code=404, detail="R2C tablet is not connected.")
    return RedirectResponse(
        url=(
            f"/{tablet.designator.lower()}/streams/"
            f"{quote(tablet.device_name, safe='')}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/s/{stream_code}")
async def connected_captured_stream_short_link(stream_code: str):
    """Resolve an ephemeral captured-stream alias to its portal path."""
    resolved = await r2c_hub.resolve_stream_link_code(stream_code)
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail="Captured stream is not available.",
        )
    tablet, stream = resolved
    return RedirectResponse(
        url=(
            f"/{tablet.designator.lower()}/streams/"
            f"{quote(tablet.device_name, safe='')}/"
            f"{quote(stream.drone_designator, safe='')}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/v/{recording_code}")
async def connected_recording_short_link(recording_code: str):
    """Resolve a stable recording alias to one exact captured video."""
    resolved = await r2c_hub.resolve_recording_link_code(recording_code)
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail="Captured recording is not available.",
        )
    tablet, stream = resolved
    return RedirectResponse(
        url=(
            f"/{tablet.designator.lower()}/streams/"
            f"{quote(tablet.device_name, safe='')}/session/"
            f"{quote(stream.session_id, safe='')}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store"},
    )


@app.get(
    "/{designator}/streams/{device_name}/session/{session_id}",
    response_class=HTMLResponse,
)
async def organization_recording_session(
        request: Request,
        designator: str,
        device_name: str,
        session_id: str):
    """Render exactly one stable recording from a connected R2C tablet."""
    tablet = await r2c_hub.resolve_connected_tablet(designator, device_name)
    if tablet is None:
        raise HTTPException(status_code=404, detail="R2C tablet not found.")
    await require_organization_user(
        request,
        tablet.designator,
        ("video_requester",),
        redirect_to_login=True,
        login_next=(
            f"/{tablet.designator.lower()}/streams/"
            f"{quote(tablet.device_name, safe='')}/session/"
            f"{quote(session_id, safe='')}"
        ),
    )
    return await organization_streams(
        request=request,
        designator=tablet.designator,
        tablet=tablet_link_code(tablet.designator, tablet.device_name),
        session=session_id,
    )


@app.get(
    "/{designator}/streams/{device_name}/{video_stream}",
    response_class=HTMLResponse,
)
async def organization_captured_stream(
        request: Request,
        designator: str,
        device_name: str,
        video_stream: str):
    """Render locally captured recordings for one tablet drone stream."""
    tablet = await r2c_hub.resolve_connected_tablet(designator, device_name)
    if tablet is None:
        raise HTTPException(status_code=404, detail="R2C tablet not found.")
    await require_organization_user(
        request,
        tablet.designator,
        ("video_requester",),
        redirect_to_login=True,
        login_next=(
            f"/{tablet.designator.lower()}/streams/"
            f"{quote(tablet.device_name, safe='')}/"
            f"{quote(video_stream, safe='')}"
        ),
    )
    return await organization_streams(
        request=request,
        designator=tablet.designator,
        tablet=tablet_link_code(tablet.designator, tablet.device_name),
        stream=video_stream,
    )


@app.get("/{designator}/streams/{device_name}", response_class=HTMLResponse)
async def organization_tablet_streams(
        request: Request,
        designator: str,
        device_name: str):
    """Render the authenticated stream catalog for one R2C tablet."""
    tablet = await r2c_hub.resolve_connected_tablet(designator, device_name)
    if tablet is None:
        return RedirectResponse(
            url=f"/{designator.lower()}/streams",
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Cache-Control": "no-store"},
        )
    tablet_code = tablet_link_code(tablet.designator, tablet.device_name)
    await require_organization_user(
        request,
        tablet.designator,
        ("video_requester",),
        redirect_to_login=True,
        login_next=(
            f"/{tablet.designator.lower()}/streams/"
            f"{quote(tablet.device_name, safe='')}"
        ),
    )
    return await organization_streams(
        request=request,
        designator=tablet.designator,
        tablet=tablet_code,
    )


@app.get("/{designator}", response_class=HTMLResponse)
async def organization_public_dashboard(
        designator: str,
        request: Request,
        response: Response,
        db: AsyncSession = Depends(get_db),
        start_date: Optional[date] = None,
        end_date: Optional[date] = None):
    """Render a public dashboard or require an organization records login."""
    if control_plane_store is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        organization = await control_plane_store.get_organization(designator)
    except InvalidOrganizationError:
        organization = None
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    if organization.records_visibility != "public":
        organization, _user = await require_organization_user(
            request,
            organization.designator,
            required_roles=(
                "organization_owner",
                "records_admin",
                "records_viewer",
            ),
            redirect_to_login=True,
            login_next=f"/{organization.designator.lower()}",
        )
        response.headers["Cache-Control"] = "no-store"
    return await render_public_dashboard(
        request=request,
        response=response,
        db=db,
        start_date=start_date,
        end_date=end_date,
        organization=organization,
    )
