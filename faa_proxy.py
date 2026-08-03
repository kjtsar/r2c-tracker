"""Narrow, authenticated proxy for the FAA NOTAM Management System API."""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth


DEFAULT_API_BASE_URL = "https://api-nms.aim.faa.gov/nmsapi"
DEFAULT_TOKEN_URL = "https://api-nms.aim.faa.gov/v1/auth/token"


class FaaProxyError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FaaProxyResponse:
    body: bytes
    cache_status: str
    age_seconds: int


@dataclass
class _CacheEntry:
    body: bytes
    stored_at: float


class FaaNotamProxy:
    """Fetches FAA NOTAM GeoJSON while keeping FAA credentials server-side."""

    def __init__(
        self,
        *,
        api_base_url: Optional[str] = None,
        token_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        cache_ttl_seconds: Optional[int] = None,
        cache_max_entries: Optional[int] = None,
        cache_max_bytes: Optional[int] = None,
        cache_max_item_bytes: Optional[int] = None,
        cache_grid_degrees: Optional[float] = None,
        request_timeout_seconds: Optional[float] = None,
        max_concurrent_upstream: Optional[int] = None,
    ):
        self.api_base_url = (
            api_base_url
            if api_base_url is not None
            else os.environ.get("FAA_NOTAM_API_BASE_URL", DEFAULT_API_BASE_URL)
        ).rstrip("/")
        self.token_url = (
            token_url
            if token_url is not None
            else os.environ.get("FAA_NOTAM_TOKEN_URL", DEFAULT_TOKEN_URL)
        )
        self.client_id = (
            client_id
            if client_id is not None
            else os.environ.get("FAA_NOTAM_CLIENT_ID", "")
        )
        self.client_secret = (
            client_secret
            if client_secret is not None
            else os.environ.get("FAA_NOTAM_CLIENT_SECRET", "")
        )
        self.cache_ttl_seconds = max(
            0,
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else int(os.environ.get("FAA_PROXY_CACHE_TTL_SEC", "90")),
        )
        self.cache_max_entries = max(
            0,
            cache_max_entries
            if cache_max_entries is not None
            else int(os.environ.get("FAA_PROXY_CACHE_MAX_ENTRIES", "512")),
        )
        self.cache_max_bytes = max(
            0,
            cache_max_bytes
            if cache_max_bytes is not None
            else int(os.environ.get("FAA_PROXY_CACHE_MAX_BYTES", str(64 * 1024 * 1024))),
        )
        self.cache_max_item_bytes = max(
            0,
            cache_max_item_bytes
            if cache_max_item_bytes is not None
            else int(os.environ.get("FAA_PROXY_CACHE_MAX_ITEM_BYTES", str(8 * 1024 * 1024))),
        )
        self.cache_grid_degrees = max(
            0.0001,
            cache_grid_degrees
            if cache_grid_degrees is not None
            else float(os.environ.get("FAA_PROXY_CACHE_GRID_DEGREES", "0.002")),
        )
        self.request_timeout_seconds = max(
            1.0,
            request_timeout_seconds
            if request_timeout_seconds is not None
            else float(os.environ.get("FAA_PROXY_REQUEST_TIMEOUT_SEC", "35")),
        )
        self.max_concurrent_upstream = max(
            1,
            max_concurrent_upstream
            if max_concurrent_upstream is not None
            else int(os.environ.get("FAA_PROXY_MAX_CONCURRENT_UPSTREAM", "8")),
        )

        self._token: Optional[str] = None
        self._token_expiry_monotonic = 0.0
        self._token_lock = threading.Lock()
        self._cache: OrderedDict[tuple, _CacheEntry] = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = asyncio.Lock()
        self._inflight: dict[tuple, asyncio.Task[bytes]] = {}
        self._upstream_semaphore = asyncio.Semaphore(self.max_concurrent_upstream)

    @property
    def configured(self) -> bool:
        return bool(
            self.api_base_url
            and self.token_url
            and self.client_id.strip()
            and self.client_secret.strip()
        )

    async def fetch_notams(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_nm: float,
        last_updated_date: Optional[str] = None,
    ) -> FaaProxyResponse:
        if not self.configured:
            raise FaaProxyError(
                "FAA NOTAM proxy credentials are not configured.",
                status_code=503,
            )

        latitude, longitude, radius_nm = self._validate_query(
            latitude, longitude, radius_nm
        )
        last_updated_date = self._normalize_last_updated_date(last_updated_date)

        # Delta requests include a caller-specific timestamp and are intentionally
        # not cached. Full queries are grouped into small geographic cells.
        if last_updated_date:
            body = await self._fetch_upstream_async(
                latitude,
                longitude,
                radius_nm,
                last_updated_date,
            )
            return FaaProxyResponse(body=body, cache_status="BYPASS", age_seconds=0)

        upstream_latitude, upstream_longitude, upstream_radius, key = (
            self._normalized_full_query(latitude, longitude, radius_nm)
        )
        now = time.monotonic()
        async with self._cache_lock:
            entry = self._cache.get(key)
            if entry is not None:
                age = max(0, int(now - entry.stored_at))
                if now - entry.stored_at <= self.cache_ttl_seconds:
                    self._cache.move_to_end(key)
                    return FaaProxyResponse(
                        body=entry.body,
                        cache_status="HIT",
                        age_seconds=age,
                    )
                removed = self._cache.pop(key, None)
                if removed is not None:
                    self._cache_bytes -= len(removed.body)

            task = self._inflight.get(key)
            cache_status = "COALESCED" if task is not None else "MISS"
            if task is None:
                task = asyncio.create_task(
                    self._fetch_upstream_async(
                        upstream_latitude,
                        upstream_longitude,
                        upstream_radius,
                        None,
                    )
                )
                self._inflight[key] = task

        try:
            body = await task
        finally:
            async with self._cache_lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

        async with self._cache_lock:
            if (
                self.cache_ttl_seconds > 0
                and self.cache_max_entries > 0
                and self.cache_max_bytes > 0
                and 0 < len(body) <= self.cache_max_item_bytes
                and len(body) <= self.cache_max_bytes
            ):
                previous = self._cache.pop(key, None)
                if previous is not None:
                    self._cache_bytes -= len(previous.body)
                self._cache[key] = _CacheEntry(body=body, stored_at=time.monotonic())
                self._cache_bytes += len(body)
                self._cache.move_to_end(key)
                while (
                    len(self._cache) > self.cache_max_entries
                    or self._cache_bytes > self.cache_max_bytes
                ):
                    _, removed = self._cache.popitem(last=False)
                    self._cache_bytes -= len(removed.body)

        return FaaProxyResponse(body=body, cache_status=cache_status, age_seconds=0)

    async def _fetch_upstream_async(
        self,
        latitude: float,
        longitude: float,
        radius_nm: float,
        last_updated_date: Optional[str],
    ) -> bytes:
        async with self._upstream_semaphore:
            return await asyncio.to_thread(
                self._fetch_upstream,
                latitude,
                longitude,
                radius_nm,
                last_updated_date,
            )

    @staticmethod
    def _validate_query(
        latitude: float, longitude: float, radius_nm: float
    ) -> tuple[float, float, float]:
        values = (latitude, longitude, radius_nm)
        if not all(math.isfinite(value) for value in values):
            raise FaaProxyError("FAA NOTAM query values must be finite.", status_code=422)
        if not -90 <= latitude <= 90:
            raise FaaProxyError("latitude must be between -90 and 90.", status_code=422)
        if not -180 <= longitude <= 180:
            raise FaaProxyError("longitude must be between -180 and 180.", status_code=422)
        if not 0 < radius_nm <= 100:
            raise FaaProxyError("radius must be greater than 0 and at most 100 NM.", status_code=422)
        return latitude, longitude, radius_nm

    @staticmethod
    def _normalize_last_updated_date(value: Optional[str]) -> Optional[str]:
        normalized = (value or "").strip()
        if not normalized:
            return None
        if len(normalized) > 64:
            raise FaaProxyError("lastUpdatedDate is too long.", status_code=422)
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FaaProxyError(
                "lastUpdatedDate must be an ISO-8601 timestamp.",
                status_code=422,
            ) from exc
        if parsed.tzinfo is None:
            raise FaaProxyError(
                "lastUpdatedDate must include a timezone.",
                status_code=422,
            )
        return normalized

    def _normalized_full_query(
        self, latitude: float, longitude: float, radius_nm: float
    ) -> tuple[float, float, float, tuple]:
        step = self.cache_grid_degrees
        cell_latitude = round(latitude / step) * step
        cell_longitude = round(longitude / step) * step
        half_lat_nm = step * 30.0
        half_lon_nm = step * 30.0 * abs(math.cos(math.radians(cell_latitude)))
        safety_margin_nm = math.hypot(half_lat_nm, half_lon_nm)

        # The expanded query prevents a cache hit from omitting a NOTAM near the
        # requesting user's radius boundary. At FAA's 100 NM limit, use the exact
        # coordinate instead because the upstream radius cannot be expanded.
        if radius_nm + safety_margin_nm <= 100:
            upstream_latitude = cell_latitude
            upstream_longitude = cell_longitude
            upstream_radius = radius_nm + safety_margin_nm
            coordinate_key = (
                round(cell_latitude, 6),
                round(cell_longitude, 6),
            )
        else:
            upstream_latitude = latitude
            upstream_longitude = longitude
            upstream_radius = radius_nm
            coordinate_key = (round(latitude, 6), round(longitude, 6))

        key = (
            coordinate_key,
            round(radius_nm, 6),
            round(upstream_radius, 6),
        )
        return upstream_latitude, upstream_longitude, upstream_radius, key

    def _get_bearer_token(self) -> str:
        now = time.monotonic()
        with self._token_lock:
            if self._token and now + 60 < self._token_expiry_monotonic:
                return self._token
            try:
                response = requests.post(
                    self.token_url,
                    auth=HTTPBasicAuth(self.client_id, self.client_secret),
                    data={"grant_type": "client_credentials"},
                    timeout=self.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                raise FaaProxyError(
                    "FAA authentication service is unavailable.",
                    status_code=503,
                ) from exc
            if response.status_code in (401, 403):
                raise FaaProxyError(
                    "FAA proxy credentials were rejected by the FAA.",
                    status_code=502,
                )
            if not response.ok:
                raise FaaProxyError(
                    f"FAA authentication failed with HTTP {response.status_code}.",
                    status_code=502,
                )
            try:
                payload = response.json()
                token = str(payload["access_token"]).strip()
                expires_in = max(60, int(payload.get("expires_in", 1800)))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise FaaProxyError(
                    "FAA authentication response was invalid.",
                    status_code=502,
                ) from exc
            if not token:
                raise FaaProxyError(
                    "FAA authentication response did not include a token.",
                    status_code=502,
                )
            self._token = token
            self._token_expiry_monotonic = now + expires_in
            return token

    def _fetch_upstream(
        self,
        latitude: float,
        longitude: float,
        radius_nm: float,
        last_updated_date: Optional[str],
    ) -> bytes:
        params = {
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "radius": f"{radius_nm:.6f}".rstrip("0").rstrip("."),
        }
        if last_updated_date:
            params["lastUpdatedDate"] = last_updated_date

        token = self._get_bearer_token()
        try:
            response = requests.get(
                f"{self.api_base_url}/v1/notams",
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "nmsResponseFormat": "GEOJSON",
                    "Accept": "application/json",
                },
                timeout=self.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise FaaProxyError(
                "FAA NOTAM service is unavailable.",
                status_code=503,
            ) from exc

        if response.status_code in (401, 403):
            with self._token_lock:
                self._token = None
                self._token_expiry_monotonic = 0
            raise FaaProxyError(
                "FAA rejected the proxy bearer token.",
                status_code=502,
            )
        if not response.ok:
            raise FaaProxyError(
                f"FAA NOTAM query failed with HTTP {response.status_code}.",
                status_code=502,
            )
        try:
            response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise FaaProxyError(
                "FAA NOTAM response was not valid JSON.",
                status_code=502,
            ) from exc
        return response.content
