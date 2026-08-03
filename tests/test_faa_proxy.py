import asyncio
import json
import unittest
from unittest.mock import patch

from faa_proxy import FaaNotamProxy, FaaProxyError


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode()
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class FaaNotamProxyTest(unittest.IsolatedAsyncioTestCase):
    def build_proxy(self, **overrides):
        values = dict(
            api_base_url="https://faa.example/nmsapi",
            token_url="https://faa.example/token",
            client_id="client",
            client_secret="secret",
            cache_ttl_seconds=90,
            cache_max_entries=2,
            cache_max_bytes=1024,
            cache_max_item_bytes=1024,
            cache_grid_degrees=0.002,
        )
        values.update(overrides)
        return FaaNotamProxy(**values)

    async def test_full_query_caches_nearby_requests_and_expands_radius(self):
        proxy = self.build_proxy()
        upstream_calls = []

        def fake_post(*args, **kwargs):
            return _FakeResponse(200, {"access_token": "bearer", "expires_in": 1800})

        def fake_get(*args, **kwargs):
            upstream_calls.append(kwargs["params"])
            return _FakeResponse(200, {"data": {"geojson": []}})

        with patch("faa_proxy.requests.post", side_effect=fake_post), patch(
            "faa_proxy.requests.get", side_effect=fake_get
        ):
            first = await proxy.fetch_notams(
                latitude=39.15301, longitude=-121.13291, radius_nm=2
            )
            second = await proxy.fetch_notams(
                latitude=39.15304, longitude=-121.13294, radius_nm=2
            )

        self.assertEqual("MISS", first.cache_status)
        self.assertEqual("HIT", second.cache_status)
        self.assertEqual(1, len(upstream_calls))
        self.assertGreater(float(upstream_calls[0]["radius"]), 2)

    async def test_concurrent_identical_queries_are_coalesced(self):
        proxy = self.build_proxy()
        calls = 0

        def blocking_fetch(*args, **kwargs):
            nonlocal calls
            calls += 1
            return b'{"data":{"geojson":[]}}'

        with patch.object(proxy, "_fetch_upstream", side_effect=blocking_fetch):
            first, second = await asyncio.gather(
                proxy.fetch_notams(latitude=39, longitude=-121, radius_nm=2),
                proxy.fetch_notams(latitude=39, longitude=-121, radius_nm=2),
            )

        self.assertEqual(1, calls)
        self.assertEqual({"MISS", "COALESCED"}, {first.cache_status, second.cache_status})

    async def test_incremental_query_bypasses_cache(self):
        proxy = self.build_proxy()
        with patch.object(
            proxy, "_fetch_upstream", return_value=b'{"data":{"geojson":[]}}'
        ) as fetch:
            result = await proxy.fetch_notams(
                latitude=39,
                longitude=-121,
                radius_nm=2,
                last_updated_date="2026-07-27T12:00:00Z",
            )
        self.assertEqual("BYPASS", result.cache_status)
        fetch.assert_called_once_with(
            39, -121, 2, "2026-07-27T12:00:00Z"
        )

    async def test_geographically_dispersed_queries_have_distinct_entries(self):
        proxy = self.build_proxy()
        with patch.object(
            proxy, "_fetch_upstream", return_value=b'{"data":{"geojson":[]}}'
        ) as fetch:
            california = await proxy.fetch_notams(
                latitude=39.15, longitude=-121.13, radius_nm=2
            )
            florida = await proxy.fetch_notams(
                latitude=25.76, longitude=-80.19, radius_nm=2
            )
        self.assertEqual("MISS", california.cache_status)
        self.assertEqual("MISS", florida.cache_status)
        self.assertEqual(2, fetch.call_count)

    async def test_invalid_and_unconfigured_queries_fail_closed(self):
        proxy = self.build_proxy(client_secret="")
        with self.assertRaises(FaaProxyError) as unconfigured:
            await proxy.fetch_notams(latitude=39, longitude=-121, radius_nm=2)
        self.assertEqual(503, unconfigured.exception.status_code)

        proxy = self.build_proxy()
        with self.assertRaises(FaaProxyError) as invalid:
            await proxy.fetch_notams(latitude=91, longitude=-121, radius_nm=2)
        self.assertEqual(422, invalid.exception.status_code)

        with self.assertRaises(FaaProxyError) as invalid_date:
            await proxy.fetch_notams(
                latitude=39,
                longitude=-121,
                radius_nm=2,
                last_updated_date="not-a-date",
            )
        self.assertEqual(422, invalid_date.exception.status_code)

    async def test_cache_is_lru_bounded(self):
        proxy = self.build_proxy(cache_max_entries=1)
        with patch.object(
            proxy, "_fetch_upstream", return_value=b'{"data":{"geojson":[]}}'
        ) as fetch:
            await proxy.fetch_notams(latitude=39, longitude=-121, radius_nm=2)
            await proxy.fetch_notams(latitude=40, longitude=-121, radius_nm=2)
            third = await proxy.fetch_notams(latitude=39, longitude=-121, radius_nm=2)
        self.assertEqual("MISS", third.cache_status)
        self.assertEqual(3, fetch.call_count)

    async def test_oversized_response_is_not_cached(self):
        proxy = self.build_proxy(cache_max_item_bytes=10)
        with patch.object(proxy, "_fetch_upstream", return_value=b"x" * 11) as fetch:
            first = await proxy.fetch_notams(latitude=39, longitude=-121, radius_nm=2)
            second = await proxy.fetch_notams(latitude=39, longitude=-121, radius_nm=2)
        self.assertEqual("MISS", first.cache_status)
        self.assertEqual("MISS", second.cache_status)
        self.assertEqual(2, fetch.call_count)


if __name__ == "__main__":
    unittest.main()
