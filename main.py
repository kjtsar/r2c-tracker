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
import warnings
import traceback
import logging
import secrets
import numpy as np
from urllib.parse import urlencode
from pprint import pprint
from datetime import datetime, date, timedelta, timezone, UTC
from zoneinfo import ZoneInfo
from timezonefinder import TimezoneFinder
from typing import Optional, Annotated
from contextlib import asynccontextmanager

from fastapi import Security, Depends, FastAPI, Request, HTTPException, Query, Form
from fastapi import status, Response, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi import BackgroundTasks
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from starlette.status import HTTP_403_FORBIDDEN
from starlette.middleware.sessions import SessionMiddleware

from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, MetaData, Table, select, text
from sqlalchemy import Column, Integer, BigInteger, String, Float, Boolean, DateTime, desc, func, or_, and_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker, Session
from suncalc import get_times
from google.cloud.sql.connector import Connector, IPTypes

# --- CONFIGURATION & DATABASE SETUP ---
DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./test.db") # Defaults to local file if no Cloud SQL
DB_USER = os.environ.get("DB_USER", "undefined")
DB_PASS = os.environ.get("DB_PASS", "undefined")
DB_NAME = os.environ.get("DB_NAME", "undefined")
API_KEY_NAME = "X-SAR-Token"
TRACKER_API_KEY = os.environ.get("TRACKER_API_KEY", "replace-with-token")
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
TRACKER_ADMIN_USER = os.environ.get("TRACKER_ADMIN_USER", "admin")
TRACKER_ADMIN_PASS = os.environ.get("TRACKER_ADMIN_PASS", "replace-with-password")
SECRET_KEY = os.environ.get("SECRET_KEY", False)

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
R2C_HEARTBEAT_SEC = int(os.environ.get("R2C_HEARTBEAT_SEC", "15"))
R2C_LEASE_SEC = int(os.environ.get("R2C_LEASE_SEC", "45"))
R2C_DB_CLEANUP_SEC = int(os.environ.get("R2C_DB_CLEANUP_SEC", "86400"))


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
class Flight(Base):
    __tablename__ = "flights"
    id = Column(Integer, primary_key=True)
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
    map_id = Column(String, index=True, nullable=False)
    zone_id = Column(String, index=True, nullable=False)
    guid = Column(String, index=True, nullable=False)
    name = Column(String, default="")
    lat = Column(Float, default=0.0)
    lng = Column(Float, default=0.0)
    caltopo_rtt_ms = Column(Integer, default=0)
    online = Column(Boolean, default=True)
    last_seen_ms = Column(BigInteger, default=0)


class R2CDroneOwnerState(Base):
    __tablename__ = "r2c_drone_owner_state"
    id = Column(Integer, primary_key=True)
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


class R2CRecentSighting(Base):
    __tablename__ = "r2c_recent_sighting"
    id = Column(Integer, primary_key=True)
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
        ("r2c_recent_sighting", "drone_ts"),
        ("r2c_recent_sighting", "received_ms"),
    ]
    async with engine.begin() as conn:
        dialect = conn.dialect.name
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
            return


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
            if "archive_relpath" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN archive_relpath VARCHAR DEFAULT ''"))
            if "remote_id" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN remote_id VARCHAR DEFAULT ''"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_flights_remote_id ON flights (remote_id)"))
        elif dialect == "sqlite":
            result = await conn.execute(text("PRAGMA table_info(flights)"))
            columns = {row[1] for row in result.fetchall()}
            if "archive_relpath" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN archive_relpath TEXT DEFAULT ''"))
            if "remote_id" not in columns:
                await conn.execute(text("ALTER TABLE flights ADD COLUMN remote_id TEXT DEFAULT ''"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_flights_remote_id ON flights (remote_id)"))


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

if __name__ == "__main__":
    asyncio.run(init_db())
    
@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup Create tables
    await init_db()
    await r2c_hub.start()
    yield
    # Shutdown Clean up resources (if needed)
    await r2c_hub.stop()
    await engine.dispose()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY
)

app.mount("/static", StaticFiles(directory="static"), name="static")


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)
        
manager = ConnectionManager()


class R2CZoneConnection:
    def __init__(self, websocket: Optional[WebSocket]):
        self.websocket = websocket
        self.map_id: Optional[str] = None
        self.zone_id: Optional[str] = None
        self.guid: Optional[str] = None
        self.name: str = ""
        self.lat: float = 0.0
        self.lng: float = 0.0
        self.caltopo_rtt_ms: int = 0
        self.connected_at_ms: int = 0
        self.hello_received_at_ms: int = 0
        self.last_seen_ms: int = 0


class R2CCoordinationHub:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._connections: dict[WebSocket, R2CZoneConnection] = {}
        self._zones_by_map: dict[str, dict[str, R2CZoneConnection]] = {}
        self._owners: dict[tuple[str, str], dict] = {}
        self._sweep_task: Optional[asyncio.Task] = None
        self._load_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._expiry_loop())
        if self._load_task is None:
            self._load_task = asyncio.create_task(self._load_state_safe())
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

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

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        async with self._lock:
            conn = R2CZoneConnection(websocket)
            conn.connected_at_ms = now_ms
            self._connections[websocket] = conn

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            conn = self._connections.pop(websocket, None)
            if conn is None:
                return
            map_id = conn.map_id
            zone_guid = conn.guid or ""
            zone_id = conn.zone_id or ""
            name = conn.name
            lat = conn.lat
            lng = conn.lng
            caltopo_rtt_ms = conn.caltopo_rtt_ms
            connected_at_ms = conn.connected_at_ms
            hello_received_at_ms = conn.hello_received_at_ms
            last_seen_ms = conn.last_seen_ms
            if map_id and zone_id:
                zones = self._zones_by_map.get(map_id, {})
                tracked = zones.get(zone_id)
                if tracked is conn:
                    conn.websocket = None
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        if map_id and zone_id:
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
                map_id,
                zone_id,
                zone_guid or zone_id,
                name,
                lat,
                lng,
                caltopo_rtt_ms,
                False,
                last_seen_ms,
            )
        if map_id:
            await self.broadcast_zone_update(map_id)

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
            await self._handle_drone_lost(payload)

    async def _handle_hello(self, websocket: WebSocket, payload: dict):
        map_id = payload.get("mapId", "")
        zone_id = payload.get("zoneId", "") or payload.get("guid", "")
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        async with self._lock:
            conn = self._connections[websocket]
            conn.map_id = map_id
            conn.zone_id = zone_id
            conn.guid = payload.get("guid", zone_id)
            conn.name = payload.get("name", zone_id)
            conn.lat = float(payload.get("lat", 0.0) or 0.0)
            conn.lng = float(payload.get("lng", 0.0) or 0.0)
            conn.caltopo_rtt_ms = self._parse_caltopo_rtt_ms(payload.get("caltopoRttMs"))
            conn.hello_received_at_ms = now_ms
            conn.last_seen_ms = now_ms
            zones = self._zones_by_map.setdefault(map_id, {})
            zones[zone_id] = conn
        logger.info(
            "r2c hello received: map=%s zone=%s guid=%s handshake_age_ms=%s",
            map_id,
            zone_id,
            conn.guid or zone_id,
            max(now_ms - int(conn.connected_at_ms or now_ms), 0),
        )
        await self._upsert_zone_state(map_id, zone_id, conn.guid or zone_id, conn.name, conn.lat, conn.lng, conn.caltopo_rtt_ms, True, now_ms)
        await websocket.send_text(json.dumps({
            "type": "hello_ack",
            "serverTime": now_ms,
            "heartbeatSec": R2C_HEARTBEAT_SEC,
            "leaseSec": R2C_LEASE_SEC
        }))
        await self.broadcast_zone_update(map_id)

    async def _handle_heartbeat(self, websocket: WebSocket, payload: dict):
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        owner_updates: list[tuple[str, str, dict]] = []
        owner_lease_expire_ms = 0
        async with self._lock:
            conn = self._connections.get(websocket)
            if conn is None:
                return
            conn.lat = float(payload.get("lat", conn.lat) or 0.0)
            conn.lng = float(payload.get("lng", conn.lng) or 0.0)
            incoming_rtt_ms = self._parse_caltopo_rtt_ms(payload.get("caltopoRttMs"))
            if incoming_rtt_ms > 0:
                conn.caltopo_rtt_ms = incoming_rtt_ms
            conn.last_seen_ms = now_ms
            map_id = conn.map_id
            zone_id = conn.zone_id
            guid = conn.guid
            name = conn.name
            lat = conn.lat
            lng = conn.lng
            caltopo_rtt_ms = conn.caltopo_rtt_ms
            if guid:
                for (owner_map_id, remote_id), owner in self._owners.items():
                    if owner.get("owner_guid") == guid:
                        owner["lease_expire_ms"] = now_ms + (R2C_LEASE_SEC * 1000)
                        owner_lease_expire_ms = max(owner_lease_expire_ms, int(owner["lease_expire_ms"]))
                        owner_updates.append((owner_map_id, remote_id, dict(owner)))
        if map_id and zone_id:
            await self._upsert_zone_state(map_id, zone_id, guid or zone_id, name, lat, lng, caltopo_rtt_ms, True, now_ms)
        for owner_map_id, remote_id, owner in owner_updates:
            await self._upsert_owner_state(owner_map_id, remote_id, owner)
        logger.info(
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
            await self.broadcast_zone_update(map_id)

    async def _handle_first_sighting(self, websocket: WebSocket, payload: dict):
        map_id = payload.get("mapId", "")
        remote_id = payload.get("remoteId", "")
        zone_id = payload.get("zoneId", "") or payload.get("guid", "")
        if not map_id or not remote_id or not zone_id:
            return
        async with self._lock:
            existing = self._owners.get((map_id, remote_id))
            candidate = {
                "owner_guid": payload.get("guid", zone_id),
                "owner_zone_id": zone_id,
                "drone_ts": int(payload.get("droneTs", 0) or 0),
                "distance_m": float(payload.get("distanceFromZoneM", 0.0) or 0.0),
                "mapped_id": payload.get("mappedId", "") or "",
                "lease_seq": 1
            }
            decision_reason = "initial_claim"
            if existing is None:
                owner = candidate
            else:
                owner = self._pick_owner(existing, candidate)
                decision_reason = "candidate_better" if owner is candidate else "existing_better"
                if owner is candidate:
                    candidate["lease_seq"] = int(existing.get("lease_seq", 0)) + 1
            owner["lease_expire_ms"] = int(datetime.now(tz=UTC).timestamp() * 1000) + (R2C_LEASE_SEC * 1000)
            self._owners[(map_id, remote_id)] = owner
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
        await self._upsert_owner_state(map_id, remote_id, owner)
        await self.broadcast(
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
        map_id = payload.get("mapId", "")
        remote_id = payload.get("remoteId", "")
        if not map_id or not remote_id:
            return
        from_zone_id = payload.get("zoneId", "") or payload.get("guid", "")
        async with self._lock:
            owner = self._owners.get((map_id, remote_id))
            if owner is None:
                return
            owner_zone_id = owner.get("owner_zone_id", "")
            if from_zone_id and owner_zone_id == from_zone_id:
                return
            zones = self._zones_by_map.get(map_id, {})
            target = zones.get(owner_zone_id)
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
        relay["fromZoneId"] = from_zone_id
        await self._record_sighting(
            map_id,
            remote_id,
            relay.get("fromZoneId", ""),
            payload.get("guid", ""),
            int(payload.get("droneTs", 0) or 0),
            float(payload.get("lat", 0.0) or 0.0),
            float(payload.get("lng", 0.0) or 0.0),
            float(payload.get("altM", 0.0) or 0.0)
        )
        try:
            await target.websocket.send_text(json.dumps(relay))
        except Exception as e:
            logger.warning("relay_sighting failed for %s/%s: %s", map_id, remote_id, e)

    async def _handle_drone_lost(self, payload: dict):
        map_id = payload.get("mapId", "")
        remote_id = payload.get("remoteId", "")
        zone_id = payload.get("zoneId", "")
        if not map_id or not remote_id:
            return
        expired = False
        async with self._lock:
            owner = self._owners.get((map_id, remote_id))
            if owner and owner.get("owner_zone_id") == zone_id:
                self._owners.pop((map_id, remote_id), None)
                expired = True
        if expired:
            logger.info(
                "r2c owner_expired: map=%s remote_id=%s reason=drone_lost prev_owner_guid=%s prev_zone_id=%s",
                map_id,
                remote_id,
                payload.get("guid", zone_id),
                zone_id,
            )
            await self._delete_owner_state(map_id, remote_id)
            await self.broadcast(
                map_id,
                {
                    "type": "owner_expired",
                    "remoteId": remote_id,
                    "prevOwnerGuid": payload.get("guid", zone_id)
                }
            )

    async def broadcast_zone_update(self, map_id: str):
        async with self._lock:
            zones = list(self._zones_by_map.get(map_id, {}).values())
        await self.broadcast(
            map_id,
            {
                "type": "zone_update",
                "zones": [
                    {
                        "zoneId": zone.zone_id,
                        "guid": zone.guid,
                        "name": zone.name,
                        "lat": zone.lat,
                        "lng": zone.lng,
                        "caltopoRttMs": zone.caltopo_rtt_ms,
                        "lastSeenMs": zone.last_seen_ms,
                        "online": zone.websocket is not None
                    }
                    for zone in zones if zone.zone_id
                ]
            }
        )

    async def broadcast(self, map_id: str, payload: dict):
        text = json.dumps(payload)
        async with self._lock:
            recipients = [zone for zone in self._zones_by_map.get(map_id, {}).values() if zone.websocket is not None]
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
            await session.commit()
        async with self._lock:
            self._zones_by_map.clear()
            for zone in zones:
                conn = R2CZoneConnection(None)
                conn.map_id = zone.map_id
                conn.zone_id = zone.zone_id
                conn.guid = zone.guid
                conn.name = zone.name
                conn.lat = zone.lat
                conn.lng = zone.lng
                conn.caltopo_rtt_ms = zone.caltopo_rtt_ms
                conn.last_seen_ms = zone.last_seen_ms
                self._zones_by_map.setdefault(zone.map_id, {})[zone.zone_id] = conn
            self._owners = {
                (owner.map_id, owner.remote_id): {
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
        if deleted_zone_count or deleted_owner_count or deleted_sighting_count:
            logger.info(
                "R2C persisted-state cleanup removed zones=%s owners=%s sightings=%s",
                deleted_zone_count,
                deleted_owner_count,
                deleted_sighting_count,
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
        expired_maps: set[str] = set()
        expired_owners: list[tuple[str, str, str]] = []
        async with self._lock:
            for map_id, zones in list(self._zones_by_map.items()):
                for zone_id, zone in list(zones.items()):
                    if zone.websocket is not None:
                        continue
                    if zone.last_seen_ms < stale_cutoff_ms:
                        zones.pop(zone_id, None)
                        expired_maps.add(map_id)
                if not zones:
                    self._zones_by_map.pop(map_id, None)
            for (map_id, remote_id), owner in list(self._owners.items()):
                if int(owner.get("lease_expire_ms", 0) or 0) < now_ms:
                    expired_owners.append((map_id, remote_id, owner.get("owner_guid", "")))
                    self._owners.pop((map_id, remote_id), None)
        for map_id in expired_maps:
            await self._delete_stale_zones(map_id, stale_cutoff_ms)
            await self.broadcast_zone_update(map_id)
        for map_id, remote_id, owner_guid in expired_owners:
            logger.info(
                "r2c owner_expired: map=%s remote_id=%s reason=lease_timeout prev_owner_guid=%s",
                map_id,
                remote_id,
                owner_guid,
            )
            await self._delete_owner_state(map_id, remote_id)
            await self.broadcast(
                map_id,
                {
                    "type": "owner_expired",
                    "remoteId": remote_id,
                    "prevOwnerGuid": owner_guid
                }
            )

    async def _upsert_zone_state(self, map_id: str, zone_id: str, guid: str, name: str,
                                 lat: float, lng: float, caltopo_rtt_ms: int,
                                 online: bool, last_seen_ms: int):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CZoneState).where(
                    R2CZoneState.map_id == map_id,
                    R2CZoneState.zone_id == zone_id
                )
            )
            state = result.scalar_one_or_none()
            if state is None:
                state = R2CZoneState(map_id=map_id, zone_id=zone_id)
                session.add(state)
            state.guid = guid
            state.name = name
            state.lat = lat
            state.lng = lng
            state.caltopo_rtt_ms = caltopo_rtt_ms
            state.online = online
            state.last_seen_ms = last_seen_ms
            await session.commit()

    async def _delete_zone_state(self, map_id: str, zone_id: str):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CZoneState).where(
                    R2CZoneState.map_id == map_id,
                    R2CZoneState.zone_id == zone_id
                )
            )
            state = result.scalar_one_or_none()
            if state is not None:
                await session.delete(state)
                await session.commit()

    async def _delete_stale_zones(self, map_id: str, cutoff_ms: int):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CZoneState).where(
                    R2CZoneState.map_id == map_id,
                    R2CZoneState.last_seen_ms < cutoff_ms
                )
            )
            for state in result.scalars().all():
                await session.delete(state)
            await session.commit()

    async def _upsert_owner_state(self, map_id: str, remote_id: str, owner: dict):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CDroneOwnerState).where(
                    R2CDroneOwnerState.map_id == map_id,
                    R2CDroneOwnerState.remote_id == remote_id
                )
            )
            state = result.scalar_one_or_none()
            if state is None:
                state = R2CDroneOwnerState(map_id=map_id, remote_id=remote_id)
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

    async def _delete_owner_state(self, map_id: str, remote_id: str):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(R2CDroneOwnerState).where(
                    R2CDroneOwnerState.map_id == map_id,
                    R2CDroneOwnerState.remote_id == remote_id
                )
            )
            state = result.scalar_one_or_none()
            if state is not None:
                await session.delete(state)
                await session.commit()

    async def _record_sighting(self, map_id: str, remote_id: str, zone_id: str, guid: str,
                               drone_ts: int, lat: float, lng: float, alt_m: float):
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        async with AsyncSessionLocal() as session:
            session.add(R2CRecentSighting(
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


r2c_hub = R2CCoordinationHub()



# --- CORS MIDDLEWARE ---
# This allows your drone app to send PUT requests without being blocked
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# --- HELPER FUNCTIONS ---
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
        
security = HTTPBasic()
def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
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

def opt_check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials is None:
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
    try:
        res = requests.get(url).json()
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

async def get_api_key(header_value: str = Depends(api_key_header)):
    if _normalize_tracker_token(header_value) == _normalize_tracker_token(TRACKER_API_KEY):
        return header_value
    else:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials"
            )

async def archive_flight_log(title, flight_timestamp, geojson_data, flight_id: int):
    relpath = archive_relpath_for_flight(flight_id, title, flight_timestamp)
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


async def create_flight_and_archive(db: AsyncSession, data: dict, flight_inputs: dict):
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
    )
    new_flight.archive_relpath = archive_relpath
    return new_flight, archive_path


async def create_imported_flight_and_archive(db: AsyncSession, data: dict, flight_inputs: dict):
    spec = flight_inputs["spec"]
    remote_id = normalize_remote_id(spec.get('rid'))
    timeofday_str = get_time_of_day(
        flight_inputs["start_ts_sec"],
        flight_inputs["start_lat"],
        flight_inputs["start_lng"],
    )

    data['r2c-tracker'] = flight_inputs["processing_comments"]
    new_flight = Flight(
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
    )
    new_flight.archive_relpath = archive_relpath
    return new_flight, archive_path


    
def format_datetime(value):
    if value is None:
        return ""
    return value.strftime('%d%b%y@%H:%M:%S-%Z')

def datetime_from_format(fmtstr):
    return datetime.strptime(fmtstr, '%d%b%y@%H:%M:%S-%Z')

def to_iso_naive(dt):
    if dt is None:
        return ""
    return dt.isoformat()

def get_flashed_messages(request: Request):
    return request.session.pop("_messages") if "_messages" in request.session else []

templates.env.globals.update(get_flashed_messages=get_flashed_messages)
templates.env.globals["tracker_version"] = TRACKER_VERSION

def flash(request: Request, message: str, category: str = "info"):
    if "_messages" not in request.session:
        request.session["_messages"] = []
    request.session["_messages"].append({"message": message, "category": category})

def admin_url(start_date: Optional[date] = None, end_date: Optional[date] = None, **extra_params) -> str:
    params = {}
    if start_date:
        params["start_date"] = start_date.isoformat()
    if end_date:
        params["end_date"] = end_date.isoformat()
    params.update({key: value for key, value in extra_params.items() if value is not None})
    return f"/admin?{urlencode(params)}" if params else "/admin"

def export_url(start_date: Optional[date] = None, end_date: Optional[date] = None) -> str:
    params = {}
    if start_date:
        params["start_date"] = start_date.isoformat()
    if end_date:
        params["end_date"] = end_date.isoformat()
    return f"/export?{urlencode(params)}" if params else "/export"


def format_elapsed_ms(elapsed_ms: Optional[int]) -> str:
    if elapsed_ms is None:
        return "unknown"
    elapsed_ms = max(int(elapsed_ms), 0)
    total_seconds = elapsed_ms // 1000
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}m {seconds:02d}s"
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}m"


def build_r2c_snapshot(zones, owners, now_ms: int):
    owner_rows_by_zone = {}
    for owner in owners:
        owner_rows_by_zone.setdefault((owner.map_id, owner.owner_zone_id), []).append({
            "remote_id": owner.remote_id,
            "mapped_id": owner.mapped_id,
            "lease_seq": owner.lease_seq,
            "lease_expire_ms": owner.lease_expire_ms,
            "lease_remaining_ms": max(int(owner.lease_expire_ms or 0) - now_ms, 0),
            "lease_remaining_label": format_elapsed_ms(int(owner.lease_expire_ms or 0) - now_ms),
            "first_drone_ts": owner.first_drone_ts,
            "first_distance_m": owner.first_distance_m,
        })

    maps = {}
    for zone in zones:
        elapsed_ms = max(now_ms - int(zone.last_seen_ms or 0), 0)
        is_online = bool(getattr(zone, "online", True))
        if not is_online:
            zone_status = "disconnected"
        elif elapsed_ms <= (R2C_HEARTBEAT_SEC * 1000 * 2):
            zone_status = "online"
        else:
            zone_status = "quiet"
        owned_drones = sorted(
            owner_rows_by_zone.get((zone.map_id, zone.zone_id), []),
            key=lambda row: (
                0 if row["mapped_id"] else 1,
                row["mapped_id"] or row["remote_id"],
                row["remote_id"],
            ),
        )
        zone_entry = {
            "map_id": zone.map_id,
            "zone_id": zone.zone_id,
            "guid": zone.guid,
            "name": zone.name or zone.zone_id,
            "lat": zone.lat,
            "lng": zone.lng,
            "caltopo_rtt_ms": zone.caltopo_rtt_ms,
            "online": is_online,
            "last_seen_ms": zone.last_seen_ms,
            "last_seen_age_ms": elapsed_ms,
            "last_seen_age_label": format_elapsed_ms(elapsed_ms),
            "status": zone_status,
            "owned_drones": owned_drones,
            "owned_drone_count": len(owned_drones),
        }
        maps.setdefault(zone.map_id, []).append(zone_entry)

    snapshot_maps = []
    for map_id in sorted(maps.keys()):
        zone_entries = sorted(
            maps[map_id],
            key=lambda row: (
                row["status"] != "online",
                row["name"].lower(),
                row["zone_id"].lower(),
            ),
        )
        snapshot_maps.append({
            "map_id": map_id,
            "zones": zone_entries,
            "zone_count": len(zone_entries),
            "owned_drone_count": sum(zone["owned_drone_count"] for zone in zone_entries),
        })

    return {
        "maps": snapshot_maps,
        "map_count": len(snapshot_maps),
        "zone_count": sum(entry["zone_count"] for entry in snapshot_maps),
        "owned_drone_count": sum(entry["owned_drone_count"] for entry in snapshot_maps),
    }

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


def archive_relpath_for_flight(flight_id: int, title: str, flight_timestamp: datetime) -> str:
    year = flight_timestamp.strftime("%Y")
    month = flight_timestamp.strftime("%m")
    filename = archive_filename_for_flight(flight_id, title, flight_timestamp)
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


async def find_overlap(db, start_time, end_time, remote_id: Optional[str] = None, sar_id: Optional[str] = None):
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

    stmt = select(Flight).filter(or_(*identity_filters)).filter(
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

# --- ROUTES ---
@app.put("/upload")
async def upload(
        request: Request,
        db: AsyncSession = Depends(get_db)):
    api_key: str = Depends(get_api_key) # activate the token check.
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
        new_flight, archive_path = await create_flight_and_archive(db, data, flight_inputs)
        await db.commit()

    await manager.broadcast("refresh") # Tell everyone to reload
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

@app.get("/", response_class=HTMLResponse)
async def public_dashboard(
        request: Request,
        response: Response,
        db: AsyncSession = Depends(get_db),
        start_date: Optional[date] = None,
        end_date: Optional[date] = None):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"

    # Base query:
    stmt = apply_date_filter(select(Flight), start_date, end_date)

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
            "flights": flights,
            "timezone" : ZoneInfo("America/Los_Angeles"),
            "leaderboard": leaderboard,
            "start_date" : start_date,
            "end_date" : end_date
        },
    )


@app.get("/r2c", response_class=HTMLResponse)
async def public_r2c_snapshot(
        request: Request,
        response: Response,
        db: AsyncSession = Depends(get_db)):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"

    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    zone_stmt = select(R2CZoneState).where(R2CZoneState.online == True).order_by(R2CZoneState.map_id, R2CZoneState.name, R2CZoneState.zone_id)
    owner_stmt = select(R2CDroneOwnerState).where(R2CDroneOwnerState.lease_expire_ms >= now_ms)

    zone_result = await db.execute(zone_stmt)
    owner_result = await db.execute(owner_stmt)

    zones = zone_result.scalars().all()
    owners = owner_result.scalars().all()
    snapshot = build_r2c_snapshot(zones, owners, now_ms)

    return templates.TemplateResponse(
        request=request,
        name="r2c_snapshot.html",
        context={
            "request": request,
            "snapshot": snapshot,
            "generated_at": datetime.now(tz=UTC),
            "generated_at_ms": now_ms,
            "heartbeat_sec": R2C_HEARTBEAT_SEC,
            "lease_sec": R2C_LEASE_SEC,
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

# List the admin page
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
        request: Request,
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin),
        start_date: Optional[date] = None,
        end_date: Optional[date] = None):

    # Base query:
    stmt = apply_date_filter(select(Flight), start_date, end_date)
    stmt = stmt.order_by(Flight.start_time.desc())
    if not start_date and not end_date:
        stmt = stmt.limit(50)
    result = await db.execute(stmt)
    flights = result.scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "request": request,
            "flights": flights,
            "start_date": start_date.isoformat() if start_date else "",
            "end_date": end_date.isoformat() if end_date else "",
            "export_url": export_url(start_date, end_date),
        },
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
    
    result = await db.execute(select(Flight).filter(Flight.id == flight_id))
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

    result = await db.execute(select(Flight).filter(Flight.id == flight_id))
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

    result = await db.execute(select(Flight).where(Flight.id.in_(flight_ids)))
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
    
    await db.execute(text("TRUNCATE TABLE flights RESTART IDENTITY CASCADE;"))
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
    stmt = apply_date_filter(select(Flight), start_date, end_date)

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

    result = await db.execute(select(Flight))
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

        content = await file.read()
        archive_bytes = io.BytesIO(content)
        with tarfile.open(fileobj=archive_bytes, mode="r:*") as tar:
            members = [
                member for member in tar.getmembers()
                if member.isfile() and member.name.endswith(".json") and "flightlog_" in os.path.basename(member.name)
            ]
            members.sort(key=lambda member: member.name)

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

    flight_result = await db.execute(select(Flight.id, Flight.archive_relpath))
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
            tar.add(BASE_LOG_DIRECTORY, arcname=os.path.basename(BASE_LOG_DIRECTORY))
        bg_tasks.add_task(os.unlink, temp_archive_path)
        return FileResponse(temp_archive_path, media_type="application/gzip", filename=archive_filename)

    except Exception as e:
        print(f"Error creating archive: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create archive: {e}")

@app.get("/flightlogs/archive/current-year", response_class=Response, responses={
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
        os.unlink(temp_archive_path)
        bg_tasks.add_task(os.unlink, temp_archive_path)
        return FileResponse(temp_archive_path, media_type="application/gzip", filename=archive_filename)

    except Exception as e:
        print(f"Error creating current year archive: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create current year archive: {e}")

    
        
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text() # Keep connection alive
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.websocket("/ws/r2c")
async def r2c_websocket_endpoint(websocket: WebSocket):
    token = websocket.headers.get(API_KEY_NAME)
    if _normalize_tracker_token(token) != _normalize_tracker_token(TRACKER_API_KEY):
        client_host = websocket.client.host if websocket.client else "unknown"
        logger.warning(
            "r2c websocket auth rejected: client=%s user_agent=%s %s",
            client_host,
            websocket.headers.get("user-agent", ""),
            _describe_tracker_token_mismatch(token, TRACKER_API_KEY),
        )
        await websocket.close(code=1008, reason="Invalid tracker token")
        return
    client_host = websocket.client.host if websocket.client else "unknown"
    await r2c_hub.connect(websocket)
    logger.info(
        "r2c websocket connected: client=%s user_agent=%s",
        client_host,
        websocket.headers.get("user-agent", ""),
    )
    try:
        while True:
            payload = json.loads(await websocket.receive_text())
            if isinstance(payload, dict):
                await r2c_hub.handle_message(websocket, payload)
    except WebSocketDisconnect as e:
        conn_info = await r2c_hub.get_connection_debug_info(websocket)
        logger.info(
            "r2c websocket closed: client=%s code=%s reason=%s map=%s zone=%s guid=%s conn_age_ms=%s hello_age_ms=%s last_seen_age_ms=%s",
            client_host,
            getattr(e, "code", ""),
            getattr(e, "reason", ""),
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
