import unittest
from typing import Any

from action0.client import APIClient
from action0.client import CachePolicy
from action0.client import JsonOperation
from action0.client import MemoryCache
from action0.client import query
from action0.client.caching import CachingAsyncBackend
from action0.client.caching import CachingDeferredBackend
from action0.client.caching import CachingSyncBackend
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend
from action0.client.testing import StubBackend
from action0.client.testing import deferred_result
from action0.req import Request
from action0.req import Response
from action0.req.body import BytesBody


class Rates(JsonOperation[Any]):
    """The example operation used in the integration tests."""

    path = "/rates"
    currency: "str | None" = query(default=None)


class FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class MemoryCacheTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.caching.MemoryCache`
    """

    def test_entries_expire(self) -> None:
        """
        Test the per-entry TTL against an injected clock.
        """
        clock = FakeClock()
        cache = MemoryCache(clock=clock)
        cache.set("k", Response(200), ttl=10)

        self.assertIsNotNone(cache.get("k"))
        clock.now += 11
        self.assertIsNone(cache.get("k"))
        self.assertEqual(len(cache), 0)

    def test_least_recently_used_entry_is_evicted(self) -> None:
        """
        Test the LRU eviction order, including the get-refreshes-recency
        rule.
        """
        cache = MemoryCache(maxsize=2)
        cache.set("a", Response(200), ttl=60)
        cache.set("b", Response(201), ttl=60)
        cache.get("a")  # refresh a, so b is now the oldest
        cache.set("c", Response(202), ttl=60)

        self.assertIsNotNone(cache.get("a"))
        self.assertIsNone(cache.get("b"))
        self.assertIsNotNone(cache.get("c"))

    def test_non_positive_ttl_stores_nothing(self) -> None:
        """
        Test that ttl<=0 is a no-op, and maxsize is validated.
        """
        cache = MemoryCache()
        cache.set("k", Response(200), ttl=0)
        self.assertIsNone(cache.get("k"))
        with self.assertRaises(ValueError):
            MemoryCache(maxsize=0)

    def test_clear(self) -> None:
        """
        Test dropping all entries.
        """
        cache = MemoryCache()
        cache.set("k", Response(200), ttl=60)
        cache.clear()
        self.assertIsNone(cache.get("k"))


class CachePolicyTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.caching.CachePolicy`
    """

    def test_keys_distinguish_method_url_and_vary_headers(self) -> None:
        """
        Test the cache key ingredients.
        """
        policy = CachePolicy()
        base = Request("https://api.example.com/rates?c=eur")

        self.assertEqual(policy.key_for(base), policy.key_for(base.copy()))
        self.assertNotEqual(policy.key_for(base), policy.key_for(base.copy(method="HEAD")))
        self.assertNotEqual(
            policy.key_for(base),
            policy.key_for(Request("https://api.example.com/rates?c=usd")),
        )
        german = base.copy(headers={"Accept-Language": "de"})
        self.assertNotEqual(policy.key_for(base), policy.key_for(german))
        # headers outside vary_headers don't fragment the cache
        tagged = base.copy(headers={"X-Trace": "abc"})
        self.assertEqual(policy.key_for(base), policy.key_for(tagged))

    def test_lookup_and_store_gates(self) -> None:
        """
        Test the method/status gates and the streaming-body exclusion.
        """
        policy = CachePolicy()
        get = Request("https://api.example.com/rates")
        post = get.copy(method="POST")

        self.assertTrue(policy.should_lookup(get))
        self.assertFalse(policy.should_lookup(post))
        self.assertTrue(policy.should_store(get, Response(200, body="x")))
        self.assertFalse(policy.should_store(get, Response(404)))
        self.assertFalse(policy.should_store(post, Response(200)))
        streamed = Response(200, body=BytesBody(b"stream"))
        self.assertFalse(policy.should_store(get, streamed))


class CachingSyncBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.caching.CachingSyncBackend`
    """

    def test_second_send_is_served_from_the_cache(self) -> None:
        """
        Test the hit path, including the request back-reference of the
        copy.
        """
        inner = StubBackend(Response(200, body="fetched"))
        backend = CachingSyncBackend(inner)
        first_request = Request("https://api.example.com/rates")
        second_request = Request("https://api.example.com/rates")

        first = backend.send(first_request)
        second = backend.send(second_request)

        self.assertEqual(len(inner.requests), 1)
        self.assertEqual(second.body_str(), "fetched")
        self.assertIs(second.request, second_request)
        self.assertIsNot(second, first)

    def test_cache_hits_are_independent_copies(self) -> None:
        """
        Test that mutating a served response cannot corrupt the cache.
        """
        backend = CachingSyncBackend(StubBackend(Response(200, headers={"X-N": "1"})))
        request = Request("https://api.example.com/rates")

        backend.send(request).headers["X-N"] = "mutated"
        backend.send(request).headers["X-N"] = "also mutated"

        self.assertEqual(backend.send(request).headers["X-N"], "1")

    def test_different_urls_do_not_collide(self) -> None:
        """
        Test the miss path for distinct cache keys.
        """
        inner = StubBackend(Response(200, body="eur"), Response(200, body="usd"))
        backend = CachingSyncBackend(inner)

        eur = backend.send(Request("https://api.example.com/rates?c=eur"))
        usd = backend.send(Request("https://api.example.com/rates?c=usd"))

        self.assertEqual([eur.body_str(), usd.body_str()], ["eur", "usd"])
        self.assertEqual(len(inner.requests), 2)

    def test_expired_entries_are_refetched(self) -> None:
        """
        Test the TTL end to end, with an injected clock.
        """
        clock = FakeClock()
        inner = StubBackend(Response(200, body="old"), Response(200, body="new"))
        backend = CachingSyncBackend(inner, CachePolicy(ttl=30), store=MemoryCache(clock=clock))
        request = Request("https://api.example.com/rates")

        self.assertEqual(backend.send(request).body_str(), "old")
        clock.now += 31
        self.assertEqual(backend.send(request).body_str(), "new")
        self.assertEqual(len(inner.requests), 2)

    def test_non_cacheable_requests_and_responses_bypass(self) -> None:
        """
        Test that POSTs and non-200s always hit the network.
        """
        inner = StubBackend(Response(200, body="x"))
        backend = CachingSyncBackend(inner)
        post = Request("https://api.example.com/rates", "POST")
        backend.send(post)
        backend.send(post)
        self.assertEqual(len(inner.requests), 2)

        failing = StubBackend(Response(503))
        backend = CachingSyncBackend(failing)
        get = Request("https://api.example.com/rates")
        backend.send(get)
        backend.send(get)
        self.assertEqual(len(failing.requests), 2)

    def test_api_client_integration(self) -> None:
        """
        Test that the wrapper composes with APIClient like any sync
        backend.
        """
        inner = StubBackend(Response(200, body='{"eur": 1.1}'))
        client = APIClient(CachingSyncBackend(inner), "https://api.example.com")

        self.assertEqual(client.send(Rates(currency="eur")), {"eur": 1.1})
        self.assertEqual(client.send(Rates(currency="eur")), {"eur": 1.1})
        self.assertEqual(len(inner.requests), 1)

    def test_store_and_inner_stay_reachable(self) -> None:
        """
        Test the store/inner properties and the repr.
        """
        inner = StubBackend()
        backend = CachingSyncBackend(inner)
        self.assertIs(backend.inner, inner)
        backend.send(Request("https://api.example.com/rates"))
        assert isinstance(backend.store, MemoryCache)
        self.assertEqual(len(backend.store), 1)
        self.assertEqual(repr(backend), "CachingSyncBackend(StubBackend(1 requests))")


class CachingAsyncBackendTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for :py:class:`action0.client.caching.CachingAsyncBackend`
    """

    async def test_second_send_is_served_from_the_cache(self) -> None:
        """
        Test the async hit path through APIClient.
        """
        inner = AsyncStubBackend(Response(200, body='{"eur": 1.1}'))
        client = APIClient(CachingAsyncBackend(inner), "https://api.example.com")

        self.assertEqual(await client.send(Rates()), {"eur": 1.1})
        self.assertEqual(await client.send(Rates()), {"eur": 1.1})
        self.assertEqual(len(inner.requests), 1)

    async def test_posts_bypass_the_cache(self) -> None:
        """
        Test the async bypass path.
        """
        inner = AsyncStubBackend(Response(200))
        backend = CachingAsyncBackend(inner)
        post = Request("https://api.example.com/rates", "POST")
        await backend.send(post)
        await backend.send(post)
        self.assertEqual(len(inner.requests), 2)


class CachingDeferredBackendTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.caching.CachingDeferredBackend`
    """

    def test_second_send_is_served_from_the_cache(self) -> None:
        """
        Test the Deferred hit path (hits fire synchronously).
        """
        inner = DeferredStubBackend(Response(200, body='{"eur": 1.1}'))
        client = APIClient(CachingDeferredBackend(inner), "https://api.example.com")

        self.assertEqual(deferred_result(client.send(Rates())), {"eur": 1.1})
        self.assertEqual(deferred_result(client.send(Rates())), {"eur": 1.1})
        self.assertEqual(len(inner.requests), 1)

    def test_failures_are_not_cached(self) -> None:
        """
        Test that a failing send leaves the cache empty.
        """

        def explode(request: Request) -> Response:
            raise ConnectionResetError("nope")

        inner = DeferredStubBackend(explode)
        backend = CachingDeferredBackend(inner)
        request = Request("https://api.example.com/rates")

        with self.assertRaises(ConnectionResetError):
            deferred_result(backend.send(request))
        with self.assertRaises(ConnectionResetError):
            deferred_result(backend.send(request))
        self.assertEqual(len(inner.requests), 2)
