import asyncio
import logging
import re
import time
from collections.abc import Callable

import requests


logger = logging.getLogger(__name__)

_SUPPORTED_URL = re.compile(r"^(?:stun|turn|turns):", re.IGNORECASE)
_PORT_53 = re.compile(r":53(?:\?|$)", re.IGNORECASE)


def sanitize_ice_servers(configured: object) -> list[dict[str, object]]:
    if not isinstance(configured, list):
        return []
    result: list[dict[str, object]] = []
    for item in configured[:8]:
        if not isinstance(item, dict):
            continue
        urls_value = item.get("urls")
        if isinstance(urls_value, str):
            source_urls = [urls_value]
            preserve_string = True
        elif isinstance(urls_value, list):
            source_urls = urls_value[:8]
            preserve_string = False
        else:
            continue
        clean_urls = [
            str(url).strip()
            for url in source_urls
            if str(url).strip()
            and _SUPPORTED_URL.match(str(url).strip())
            and not _PORT_53.search(str(url).strip())
        ]
        if not clean_urls:
            continue
        clean_item: dict[str, object] = {
            "urls": clean_urls[0] if preserve_string else clean_urls,
        }
        username = str(item.get("username", "") or "").strip()
        credential = str(item.get("credential", "") or "").strip()
        if username and credential:
            clean_item["username"] = username
            clean_item["credential"] = credential
        result.append(clean_item)
    return result


class CloudflareTurnCredentialProvider:
    def __init__(
        self,
        *,
        key_id: str,
        api_token: str,
        fallback_ice_servers: list[dict[str, object]],
        credential_ttl_seconds: int = 3600,
        post: Callable[..., requests.Response] = requests.post,
    ):
        self.key_id = key_id.strip()
        self.api_token = api_token.strip()
        self.fallback_ice_servers = sanitize_ice_servers(fallback_ice_servers)
        self.credential_ttl_seconds = max(
            300,
            min(int(credential_ttl_seconds), 172_800),
        )
        self._post = post
        self._lock = asyncio.Lock()
        self._cached: list[dict[str, object]] = []
        self._cached_until = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(self.key_id and self.api_token)

    async def get_ice_servers(self) -> list[dict[str, object]]:
        if not self.is_configured:
            return list(self.fallback_ice_servers)
        now = time.monotonic()
        if self._cached and now < self._cached_until:
            return list(self._cached)
        async with self._lock:
            now = time.monotonic()
            if self._cached and now < self._cached_until:
                return list(self._cached)
            try:
                request_started = time.monotonic()
                response = await asyncio.to_thread(
                    self._post,
                    "https://rtc.live.cloudflare.com/v1/turn/keys/"
                    f"{self.key_id}/credentials/generate-ice-servers",
                    headers={
                        "Authorization": f"Bearer {self.api_token}",
                        "Content-Type": "application/json",
                    },
                    json={"ttl": self.credential_ttl_seconds},
                    timeout=8,
                )
                response.raise_for_status()
                generated = sanitize_ice_servers(
                    response.json().get("iceServers", [])
                )
                if not any(
                    str(url).lower().startswith(("turn:", "turns:"))
                    for server in generated
                    for url in (
                        server["urls"]
                        if isinstance(server.get("urls"), list)
                        else [server.get("urls", "")]
                    )
                ):
                    raise ValueError("credential response did not include TURN")
                self._cached = generated
                self._cached_until = now + max(
                    30,
                    min(
                        self.credential_ttl_seconds * 0.8,
                        self.credential_ttl_seconds - 60,
                    ),
                )
                logger.info(
                    "Cloudflare TURN credentials generated in %d ms",
                    round((time.monotonic() - request_started) * 1000),
                )
                return list(self._cached)
            except Exception:
                logger.warning(
                    "Cloudflare TURN credential generation failed; using STUN fallback",
                    exc_info=True,
                )
                return list(self.fallback_ice_servers)
