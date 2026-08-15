import asyncio
import logging
import re
import time
from collections.abc import Callable

import requests


logger = logging.getLogger(__name__)

_SUPPORTED_URL = re.compile(r"^(?:stun|turn|turns):", re.IGNORECASE)
_PORT_53 = re.compile(r":53(?:\?|$)", re.IGNORECASE)
_CUSTOM_IDENTIFIER = re.compile(r"[^A-Za-z0-9._:-]+")
_CLOUDFLARE_ICE_URLS = [
    "stun:stun.cloudflare.com:3478",
    "turn:turn.cloudflare.com:3478?transport=udp",
    "turn:turn.cloudflare.com:3478?transport=tcp",
    "turn:turn.cloudflare.com:80?transport=tcp",
    "turns:turn.cloudflare.com:5349?transport=tcp",
    "turns:turn.cloudflare.com:443?transport=tcp",
]


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
        self._cached: dict[str, tuple[list[dict[str, object]], float]] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self.key_id and self.api_token)

    async def get_ice_servers(
        self,
        custom_identifier: str = "",
    ) -> list[dict[str, object]]:
        if not self.is_configured:
            return list(self.fallback_ice_servers)
        clean_identifier = _CUSTOM_IDENTIFIER.sub(
            "-", str(custom_identifier or "").strip()
        )[:64]
        cache_key = clean_identifier
        now = time.monotonic()
        cached, cached_until = self._cached.get(cache_key, ([], 0.0))
        if cached and now < cached_until:
            return list(cached)
        async with self._lock:
            now = time.monotonic()
            cached, cached_until = self._cached.get(cache_key, ([], 0.0))
            if cached and now < cached_until:
                return list(cached)
            try:
                request_started = time.monotonic()
                endpoint = (
                    "generate" if clean_identifier else "generate-ice-servers"
                )
                payload: dict[str, object] = {
                    "ttl": self.credential_ttl_seconds,
                }
                if clean_identifier:
                    payload["customIdentifier"] = clean_identifier
                response = await asyncio.to_thread(
                    self._post,
                    "https://rtc.live.cloudflare.com/v1/turn/keys/"
                    f"{self.key_id}/credentials/{endpoint}",
                    headers={
                        "Authorization": f"Bearer {self.api_token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=8,
                )
                response.raise_for_status()
                response_payload = response.json()
                generated = sanitize_ice_servers(
                    response_payload.get("iceServers", [])
                )
                if not generated:
                    username = str(response_payload.get("username", "") or "")
                    credential = str(
                        response_payload.get("credential", "") or ""
                    )
                    generated = sanitize_ice_servers([
                        {"urls": [_CLOUDFLARE_ICE_URLS[0]]},
                        {
                            "urls": _CLOUDFLARE_ICE_URLS[1:],
                            "username": username,
                            "credential": credential,
                        },
                    ])
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
                cached_until = now + max(
                    30,
                    min(
                        self.credential_ttl_seconds * 0.8,
                        self.credential_ttl_seconds - 60,
                    ),
                )
                self._cached[cache_key] = (generated, cached_until)
                logger.info(
                    "Cloudflare TURN credentials generated in %d ms%s",
                    round((time.monotonic() - request_started) * 1000),
                    " with usage attribution" if clean_identifier else "",
                )
                return list(generated)
            except Exception:
                logger.warning(
                    "Cloudflare TURN credential generation failed; using STUN fallback",
                    exc_info=True,
                )
                return list(self.fallback_ice_servers)
