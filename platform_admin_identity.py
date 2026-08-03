import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional


PLATFORM_ADMIN_IDENTITY_SECRET = "r2c-super-admin-identity"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PlatformAdminIdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlatformAdminIdentity:
    email: str
    display_name: str
    generation: str


def parse_platform_admin_identity(payload: bytes, generation: str) -> PlatformAdminIdentity:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformAdminIdentityError(
            "The platform administrator identity secret is not valid JSON."
        ) from exc
    if not isinstance(value, dict):
        raise PlatformAdminIdentityError(
            "The platform administrator identity secret must be a JSON object."
        )
    email = str(value.get("email", "")).strip().lower()
    display_name = str(value.get("display_name", "")).strip()
    if not EMAIL_RE.fullmatch(email):
        raise PlatformAdminIdentityError(
            "The platform administrator identity secret has an invalid email."
        )
    if not display_name:
        raise PlatformAdminIdentityError(
            "The platform administrator identity secret has no display name."
        )
    if not generation:
        raise PlatformAdminIdentityError(
            "The platform administrator identity secret has no version."
        )
    return PlatformAdminIdentity(
        email=email,
        display_name=display_name,
        generation=generation,
    )


class SecretManagerPlatformAdminIdentityProvider:
    """Read the authoritative administrator from Secret Manager with a short TTL."""

    def __init__(
        self,
        *,
        project_id: Optional[str] = None,
        client=None,
        cache_ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._project_id = project_id
        self._client = client
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._cached: Optional[PlatformAdminIdentity] = None
        self._cached_until = 0.0
        self._lock = asyncio.Lock()

    def _ensure_client(self):
        if self._client is not None and self._project_id:
            return
        try:
            import google.auth
            from google.cloud import secretmanager

            credentials, detected_project = google.auth.default()
            project_id = self._project_id or detected_project
            if not project_id:
                raise PlatformAdminIdentityError(
                    "Google application credentials did not identify a project."
                )
            self._project_id = project_id
            if self._client is None:
                self._client = secretmanager.SecretManagerServiceClient(
                    credentials=credentials
                )
        except PlatformAdminIdentityError:
            raise
        except Exception as exc:
            raise PlatformAdminIdentityError(
                "Unable to initialize the Secret Manager identity provider."
            ) from exc

    def _read_latest(self) -> PlatformAdminIdentity:
        self._ensure_client()
        name = (
            f"projects/{self._project_id}/secrets/"
            f"{PLATFORM_ADMIN_IDENTITY_SECRET}/versions/latest"
        )
        try:
            response = self._client.access_secret_version(request={"name": name})
            version = str(response.name).rsplit("/", 1)[-1]
            return parse_platform_admin_identity(response.payload.data, version)
        except PlatformAdminIdentityError:
            raise
        except Exception as exc:
            raise PlatformAdminIdentityError(
                "The platform administrator identity is temporarily unavailable."
            ) from exc

    async def get_current(self) -> PlatformAdminIdentity:
        now = self._clock()
        if self._cached is not None and now < self._cached_until:
            return self._cached
        async with self._lock:
            now = self._clock()
            if self._cached is not None and now < self._cached_until:
                return self._cached
            identity = await asyncio.to_thread(self._read_latest)
            self._cached = identity
            self._cached_until = self._clock() + self._cache_ttl_seconds
            return identity
