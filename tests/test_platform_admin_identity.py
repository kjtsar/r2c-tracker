import asyncio
import json
import unittest
from types import SimpleNamespace

from platform_admin_identity import (
    PlatformAdminIdentityError,
    SecretManagerPlatformAdminIdentityProvider,
    parse_platform_admin_identity,
)


class FakeSecretManagerClient:
    def __init__(self, payload, version="1"):
        self.payload = payload
        self.version = version
        self.calls = 0

    def access_secret_version(self, request):
        self.calls += 1
        return SimpleNamespace(
            name=(
                "projects/test-project/secrets/"
                f"r2c-super-admin-identity/versions/{self.version}"
            ),
            payload=SimpleNamespace(data=self.payload),
        )


class PlatformAdminIdentityTest(unittest.TestCase):
    def test_parses_and_normalizes_identity(self):
        identity = parse_platform_admin_identity(
            json.dumps(
                {
                    "email": " Admin@Example.ORG ",
                    "display_name": "GCI Administrator",
                }
            ).encode(),
            "7",
        )

        self.assertEqual("admin@example.org", identity.email)
        self.assertEqual("GCI Administrator", identity.display_name)
        self.assertEqual("7", identity.generation)

    def test_invalid_or_incomplete_secret_fails_closed(self):
        for payload in (
            b"not-json",
            b"[]",
            b'{"email":"invalid","display_name":"Administrator"}',
            b'{"email":"admin@example.org","display_name":""}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(PlatformAdminIdentityError):
                    parse_platform_admin_identity(payload, "1")

    def test_provider_caches_briefly_then_observes_new_version(self):
        now = [100.0]
        client = FakeSecretManagerClient(
            b'{"email":"first@example.org","display_name":"First"}'
        )
        provider = SecretManagerPlatformAdminIdentityProvider(
            project_id="test-project",
            client=client,
            cache_ttl_seconds=30,
            clock=lambda: now[0],
        )

        first = asyncio.run(provider.get_current())
        client.payload = b'{"email":"second@example.org","display_name":"Second"}'
        client.version = "2"
        cached = asyncio.run(provider.get_current())
        now[0] += 31
        refreshed = asyncio.run(provider.get_current())

        self.assertEqual("first@example.org", first.email)
        self.assertEqual(first, cached)
        self.assertEqual("second@example.org", refreshed.email)
        self.assertEqual("2", refreshed.generation)
        self.assertEqual(2, client.calls)
