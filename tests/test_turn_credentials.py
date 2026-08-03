import asyncio
import unittest

import requests

from turn_credentials import (
    CloudflareTurnCredentialProvider,
    sanitize_ice_servers,
)


class FakeResponse:
    def __init__(self, payload, status_code=201):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self.payload


class TurnCredentialProviderTest(unittest.TestCase):
    def test_sanitizer_filters_port_53_and_unknown_schemes(self):
        self.assertEqual(
            [
                {"urls": ["stun:stun.cloudflare.com:3478"]},
                {
                    "urls": [
                        "turn:turn.cloudflare.com:3478?transport=udp",
                        "turns:turn.cloudflare.com:443?transport=tcp",
                    ],
                    "username": "short-lived-user",
                    "credential": "short-lived-password",
                },
            ],
            sanitize_ice_servers(
                [
                    {
                        "urls": [
                            "stun:stun.cloudflare.com:3478",
                            "stun:stun.cloudflare.com:53",
                        ]
                    },
                    {
                        "urls": [
                            "turn:turn.cloudflare.com:3478?transport=udp",
                            "turn:turn.cloudflare.com:53?transport=udp",
                            "turns:turn.cloudflare.com:443?transport=tcp",
                            "https://not-an-ice-server.example",
                        ],
                        "username": "short-lived-user",
                        "credential": "short-lived-password",
                    },
                ]
            ),
        )

    def test_generates_and_caches_short_lived_credentials(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(
                {
                    "iceServers": [
                        {"urls": ["stun:stun.cloudflare.com:3478"]},
                        {
                            "urls": [
                                "turn:turn.cloudflare.com:3478?transport=udp",
                                "turn:turn.cloudflare.com:53?transport=udp",
                                "turns:turn.cloudflare.com:443?transport=tcp",
                            ],
                            "username": "generated-user",
                            "credential": "generated-password",
                        },
                    ]
                }
            )

        provider = CloudflareTurnCredentialProvider(
            key_id="turn-key-id",
            api_token="turn-api-token",
            fallback_ice_servers=[
                {"urls": ["stun:stun.cloudflare.com:3478"]}
            ],
            credential_ttl_seconds=3600,
            post=post,
        )
        first = asyncio.run(provider.get_ice_servers())
        second = asyncio.run(provider.get_ice_servers())

        self.assertEqual(first, second)
        self.assertEqual(1, len(calls))
        self.assertEqual({"ttl": 3600}, calls[0][1]["json"])
        self.assertEqual(
            "Bearer turn-api-token",
            calls[0][1]["headers"]["Authorization"],
        )
        self.assertNotIn(":53", str(first))
        self.assertIn("turns:turn.cloudflare.com:443", str(first))

    def test_failure_returns_stun_fallback_without_exposing_secret(self):
        def post(_url, **_kwargs):
            raise requests.Timeout("provider unavailable")

        provider = CloudflareTurnCredentialProvider(
            key_id="turn-key-id",
            api_token="never-log-this-token",
            fallback_ice_servers=[
                {"urls": ["stun:stun.cloudflare.com:3478"]}
            ],
            post=post,
        )
        with self.assertLogs("turn_credentials", level="WARNING") as logs:
            result = asyncio.run(provider.get_ice_servers())

        self.assertEqual(
            [{"urls": ["stun:stun.cloudflare.com:3478"]}],
            result,
        )
        self.assertNotIn("never-log-this-token", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
