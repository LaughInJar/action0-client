import hashlib
import pickle
import unittest
from typing import Any

from action0.client import APIClient
from action0.client import BodyMode
from action0.client import CachePolicy
from action0.client import JsonOperation
from action0.client import MemoryCache
from action0.client import RequestOptions
from action0.client import json_field
from action0.client import query
from action0.client.backends.futures import ThreadPoolBackend
from action0.client.caching import AsyncCacheStore
from action0.client.caching import CachingAsyncBackend
from action0.client.caching import CachingDeferredBackend
from action0.client.caching import CachingSyncBackend
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend
from action0.client.testing import StubBackend
from action0.client.testing import deferred_result
from action0.req import AsyncIterableBody
from action0.req import IterableBody
from action0.req import Method
from action0.req import Request
from action0.req import Response
from action0.req import SpooledBody
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


class AsyncMemoryStore:
    """
    An :py:class:`~action0.client.caching.AsyncCacheStore` double: a
    MemoryCache behind awaitable methods, recording every call.
    """

    def __init__(self) -> None:
        self.inner = MemoryCache()
        self.calls: list[str] = []

    async def get(self, key: str) -> "Response | None":
        self.calls.append("get")
        return self.inner.get(key)

    async def set(self, key: str, response: Response, ttl: float) -> None:
        self.calls.append("set")
        self.inner.set(key, response, ttl)


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

    async def test_async_store_is_awaited(self) -> None:
        """
        Test the AsyncCacheStore flavor: lookups and stores are awaited,
        and the hit path still skips the network.
        """
        store: AsyncCacheStore = AsyncMemoryStore()  # pins protocol conformance
        inner = AsyncStubBackend(Response(200, body="fetched"))
        backend = CachingAsyncBackend(inner, store=store)
        request = Request("https://api.example.com/rates")

        first = await backend.send(request)
        second = await backend.send(request)

        self.assertEqual([first.body_str(), second.body_str()], ["fetched", "fetched"])
        self.assertEqual(len(inner.requests), 1)
        assert isinstance(store, AsyncMemoryStore)
        self.assertEqual(store.calls, ["get", "set", "get"])

    async def test_async_store_sees_no_uncacheable_traffic(self) -> None:
        """
        Test that the bypass path never touches an async store.
        """
        store = AsyncMemoryStore()
        inner = AsyncStubBackend(Response(200))
        backend = CachingAsyncBackend(inner, store=store)
        await backend.send(Request("https://api.example.com/rates", "POST"))
        self.assertEqual(store.calls, [])


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


class Translate(JsonOperation[Any]):
    """A POST endpoint that only reads: cacheable, for an hour."""

    method = Method.POST
    path = "/translate"
    request_options = RequestOptions(cacheable=True, cache_ttl=3600)

    text: str = json_field()


class RecordingStore:
    """A CacheStore double keeping what it is given, as given."""

    def __init__(self) -> None:
        self.entries: dict[str, Response] = {}
        self.ttls: dict[str, float] = {}

    def get(self, key: str) -> "Response | None":
        return self.entries.get(key)

    def set(self, key: str, response: Response, ttl: float) -> None:
        self.entries[key] = response
        self.ttls[key] = ttl


class KeyValueClient:
    """
    A stand-in for the client of a key-value cache server: bytes in,
    bytes out, an expiry per entry — the interface the documented
    adapters are written against.
    """

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.expiries: dict[str, int] = {}

    def get(self, key: str) -> "bytes | None":
        return self.data.get(key)

    def set(self, key: str, value: bytes, ttl: int) -> None:
        self.data[key] = value
        self.expiries[key] = ttl


class AsyncKeyValueClient:
    """The awaitable twin of :py:class:`KeyValueClient`."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.expiries: dict[str, int] = {}

    async def get(self, key: str) -> "bytes | None":
        return self.data.get(key)

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        self.data[key] = value
        self.expiries[key] = ttl


class KeyValueCache:
    """The sync adapter from the caching guide: a CacheStore over a
    key-value client, with pickled responses."""

    def __init__(self, client: KeyValueClient, prefix: str = "action0:") -> None:
        self._client = client
        self._prefix = prefix

    def get(self, key: str) -> "Response | None":
        data = self._client.get(self._prefix + key)
        return pickle.loads(data) if data is not None else None

    def set(self, key: str, response: Response, ttl: float) -> None:
        self._client.set(self._prefix + key, pickle.dumps(response), ttl=max(1, int(ttl)))


class AsyncKeyValueCache:
    """The async adapter from the caching guide."""

    def __init__(self, client: AsyncKeyValueClient, prefix: str = "action0:") -> None:
        self._client = client
        self._prefix = prefix

    async def get(self, key: str) -> "Response | None":
        data = await self._client.get(self._prefix + key)
        return pickle.loads(data) if data is not None else None

    async def set(self, key: str, response: Response, ttl: float) -> None:
        await self._client.set(self._prefix + key, pickle.dumps(response), ttl=max(1, int(ttl)))


class CacheKeyTestCase(unittest.TestCase):
    """
    tests for the cache key and the per-request cache options of
    :py:class:`action0.client.caching.CachePolicy`
    """

    URL = "https://api.example.com/translate"

    def setUp(self) -> None:
        self.policy = CachePolicy()

    def test_bodyless_keys_are_unchanged(self) -> None:
        """
        Test that the key of a request without a body is still the
        digest of method, URL and vary headers — entries written by
        earlier versions stay valid.
        """
        request = Request("https://api.example.com/rates?c=eur", headers={"Accept": "a/b"})
        lines = ["GET", "https://api.example.com/rates?c=eur", "accept: a/b", "accept-language: "]
        expected = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        self.assertEqual(self.policy.key_for(request), expected)

    def test_body_is_part_of_the_key(self) -> None:
        """
        Test the fail-safe: two requests differing only in their body
        never share a key, while the same body — as bytes or as str —
        gives the same key.
        """
        hello = Request(self.URL, "POST", body=b'{"text": "hello"}')
        world = Request(self.URL, "POST", body=b'{"text": "world"}')
        as_text = Request(self.URL, "POST", body='{"text": "hello"}')
        bodyless = Request(self.URL, "POST")
        empty = Request(self.URL, "POST", body=b"")

        self.assertNotEqual(self.policy.key_for(hello), self.policy.key_for(world))
        self.assertEqual(self.policy.key_for(hello), self.policy.key_for(as_text))
        self.assertNotEqual(self.policy.key_for(hello), self.policy.key_for(bodyless))
        self.assertNotEqual(self.policy.key_for(empty), self.policy.key_for(bodyless))

    def test_volatile_headers_stay_out_of_the_key(self) -> None:
        """
        Test that headers that change with every request — a rotated
        bearer token, a signature, a timestamp — do not fragment the
        cache, while a vary header does.
        """
        first = Request(self.URL, headers={"Authorization": "Bearer one", "X-Signature": "a1"})
        second = Request(
            self.URL,
            headers={"Authorization": "Bearer two", "X-Signature": "b2", "Date": "today"},
        )
        self.assertEqual(self.policy.key_for(first), self.policy.key_for(second))

        german = second.copy()
        german.headers["Accept-Language"] = "de"
        self.assertNotEqual(self.policy.key_for(first), self.policy.key_for(german))

    def test_identity_can_be_made_part_of_the_key(self) -> None:
        """
        Test the documented way to cache per caller: name the header
        that identifies the caller as a vary header.
        """
        policy = CachePolicy(vary_headers=("Accept", "X-Tenant"))
        one = Request(self.URL, headers={"X-Tenant": "one"})
        two = Request(self.URL, headers={"X-Tenant": "two"})
        self.assertNotEqual(policy.key_for(one), policy.key_for(two))

    def test_explicit_key_is_used_as_given(self) -> None:
        """
        Test that a request's own cache key replaces the computed one,
        whatever the rest of the request looks like.
        """
        request = Request(self.URL, "POST", body=b"volatile nonce 1")
        RequestOptions(cache_key="translate:hello:de").apply(request)
        self.assertEqual(self.policy.key_for(request), "translate:hello:de")

    def test_cacheable_overrides_the_method_gate(self) -> None:
        """
        Test both directions: a POST declared cacheable is cached, a GET
        declared not cacheable is not.
        """
        post = Request(self.URL, "POST", body=b"{}")
        get = Request(self.URL)
        self.assertFalse(self.policy.should_lookup(post))
        self.assertTrue(self.policy.should_lookup(get))

        RequestOptions(cacheable=True).apply(post)
        RequestOptions(cacheable=False).apply(get)
        self.assertTrue(self.policy.should_lookup(post))
        self.assertFalse(self.policy.should_lookup(get))
        # the status gate still applies to a cacheable POST
        self.assertTrue(self.policy.should_store(post, Response(200, body="x")))
        self.assertFalse(self.policy.should_store(post, Response(500, body="x")))
        self.assertFalse(self.policy.should_store(get, Response(200, body="x")))

    def test_streamed_request_body_needs_an_explicit_key(self) -> None:
        """
        Test the fail-safe for bodies that cannot be hashed: such a
        request is never cached under a key that ignores its body — only
        under a key it brings along.
        """
        upload = Request(self.URL, "POST", body=IterableBody([b"chunk"]))
        RequestOptions(cacheable=True).apply(upload)
        self.assertFalse(self.policy.should_lookup(upload))
        self.assertFalse(self.policy.should_store(upload, Response(200, body="x")))

        RequestOptions(cache_key="upload:42").apply(upload)
        self.assertTrue(self.policy.should_lookup(upload))
        self.assertEqual(self.policy.key_for(upload), "upload:42")

    def test_ttl_per_request(self) -> None:
        """
        Test that a request's own ttl wins over the policy's.
        """
        policy = CachePolicy(ttl=60)
        request = Request(self.URL)
        self.assertEqual(policy.ttl_for(request), 60)
        RequestOptions(cache_ttl=5).apply(request)
        self.assertEqual(policy.ttl_for(request), 5)

    def test_max_body_size(self) -> None:
        """
        Test that responses larger than the limit are not stored, that
        the limit itself still is, and that there is none by default.
        """
        request = Request(self.URL)
        limited = CachePolicy(max_body_size=4)
        self.assertTrue(limited.should_store(request, Response(200, body=b"1234")))
        self.assertFalse(limited.should_store(request, Response(200, body=b"12345")))
        # the limit counts bytes, not characters
        self.assertFalse(limited.should_store(request, Response(200, body="äää")))
        self.assertTrue(limited.should_store(request, Response(200)))
        self.assertTrue(CachePolicy().should_store(request, Response(200, body=b"x" * 10**6)))

    def test_producer_response_bodies_are_never_stored(self) -> None:
        """
        Test that neither a streamed nor a spooled response body is
        stored.
        """
        request = Request(self.URL)
        with SpooledBody() as spool:
            spool.write(b"spooled")
            self.assertFalse(self.policy.should_store(request, Response(200, body=spool)))
        streamed = Response(200, body=IterableBody([b"streamed"]))
        self.assertFalse(self.policy.should_store(request, streamed))


class StoredEntryTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for what the caching wrappers put into the store and hand out
    on a hit
    """

    URL = "https://user:hunter2@api.example.com/rates"

    def request(self, **options: Any) -> Request:
        """
        A request carrying credentials and metadata that must not end up
        in a store.

        :param options: :py:class:`RequestOptions` fields
        :return: the request
        """
        request = Request(
            self.URL,
            headers={"Authorization": "Bearer s3cr3t-token"},
            meta={"app.trace": "trace-id-1234"},
        )
        if options:
            RequestOptions(**options).apply(request)
        return request

    def test_entries_are_stored_without_the_request(self) -> None:
        """
        Test that the stored response has no request reference — so
        neither credentials nor request metadata reach the store — while
        the response handed out keeps it, on a miss and on a hit.
        """
        store = RecordingStore()
        backend = CachingSyncBackend(StubBackend(Response(200, body="rates")), store=store)

        request = self.request()
        missed = backend.send(request)
        self.assertIs(missed.request, request)

        (entry,) = store.entries.values()
        self.assertIsNone(entry.request)
        self.assertEqual(entry.body, "rates")

        again = self.request()
        hit = backend.send(again)
        self.assertIs(hit.request, again)
        self.assertIsNone(entry.request)

    def test_pickled_entries_survive_and_hold_no_secrets(self) -> None:
        """
        Test the store-outside-the-process case with the adapter from
        the guide: the entry survives the pickle round trip, expires
        with the policy's ttl, and the stored bytes contain neither the
        token, nor the URL password, nor the request metadata.
        """
        client = KeyValueClient()
        inner = StubBackend(
            Response(
                200, headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")], body=b"\x00rates"
            )
        )
        backend = CachingSyncBackend(inner, CachePolicy(ttl=90), store=KeyValueCache(client))

        backend.send(self.request())
        hit = backend.send(self.request())

        self.assertEqual(len(inner.requests), 1)
        self.assertEqual(hit.body, b"\x00rates")
        self.assertEqual(hit.headers.get_all("Set-Cookie"), ["a=1", "b=2"])
        (key,) = client.data
        self.assertTrue(key.startswith("action0:"))
        self.assertEqual(client.expiries[key], 90)
        for secret in (b"s3cr3t-token", b"hunter2", b"trace-id-1234"):
            self.assertNotIn(secret, client.data[key])

    async def test_pickled_entries_with_an_async_store(self) -> None:
        """
        Test the same through the async wrapper and the async adapter.
        """
        client = AsyncKeyValueClient()
        inner = AsyncStubBackend(Response(200, body='{"eur": 1.1}'))
        backend = CachingAsyncBackend(inner, store=AsyncKeyValueCache(client))
        api = APIClient(backend, "https://api.example.com")

        self.assertEqual(await api.send(Rates(currency="eur")), {"eur": 1.1})
        self.assertEqual(await api.send(Rates(currency="eur")), {"eur": 1.1})
        self.assertEqual(len(inner.requests), 1)
        (stored,) = client.data.values()
        self.assertIsNone(pickle.loads(stored).request)

    def test_per_request_ttl_reaches_the_store(self) -> None:
        """
        Test that the store is given the request's own ttl.
        """
        store = RecordingStore()
        backend = CachingSyncBackend(StubBackend(Response(200)), CachePolicy(ttl=60), store)
        backend.send(self.request(cache_ttl=5))
        self.assertEqual(list(store.ttls.values()), [5])

    def test_explicit_key_reaches_the_store(self) -> None:
        """
        Test that an explicit cache key is the key the store sees, and
        that two different requests sharing it share the entry.
        """
        store = RecordingStore()
        inner = StubBackend(Response(200, body="shared"))
        backend = CachingSyncBackend(inner, store=store)

        backend.send(self.request(cache_key="rates:eur"))
        other = Request("https://api.example.com/v2/rates?c=eur")
        RequestOptions(cache_key="rates:eur").apply(other)
        self.assertEqual(backend.send(other).body, "shared")
        self.assertEqual(list(store.entries), ["rates:eur"])
        self.assertEqual(len(inner.requests), 1)

    def test_oversized_responses_pass_through_uncached(self) -> None:
        """
        Test that a response over the size limit is returned but not
        stored.
        """
        inner = StubBackend(Response(200, body=b"x" * 100))
        backend = CachingSyncBackend(inner, CachePolicy(max_body_size=10))
        self.assertEqual(backend.send(self.request()).body, b"x" * 100)
        self.assertEqual(backend.send(self.request()).body, b"x" * 100)
        self.assertEqual(len(inner.requests), 2)

    def test_hit_comes_in_the_requested_body_mode(self) -> None:
        """
        Test that a hit is delivered the way the request asks for its
        body: bytes, a single-use stream, or a spool — so code written
        for a spooled body works whether or not the cache answered.
        """
        backend = CachingSyncBackend(StubBackend(Response(200, body=b"cached bytes")))
        backend.send(self.request())

        self.assertEqual(backend.send(self.request()).body, b"cached bytes")
        self.assertEqual(backend.send(self.request(body=BodyMode.PRELOAD)).body, b"cached bytes")

        streamed = backend.send(self.request(body=BodyMode.STREAM))
        self.assertIsInstance(streamed.body, IterableBody)
        self.assertEqual(streamed.body_bytes(), b"cached bytes")

        with backend.send(self.request(body=BodyMode.SPOOL, spool_max_memory=4)) as spooled:
            body = spooled.body
            assert isinstance(body, SpooledBody)
            self.assertTrue(body.rolled)
            file = spooled.body_file()
            assert file is not None
            self.assertEqual(file.read(), b"cached bytes")
        self.assertTrue(body.closed)

    async def test_async_hit_comes_in_the_requested_body_mode(self) -> None:
        """
        Test the same on the async wrapper: a streamed hit is an async
        body, a spooled one is filled without blocking.
        """
        backend = CachingAsyncBackend(AsyncStubBackend(Response(200, body="cached text")))
        await backend.send(self.request())

        streamed = await backend.send(self.request(body=BodyMode.STREAM))
        body = streamed.body
        assert isinstance(body, AsyncIterableBody)
        self.assertEqual([chunk async for chunk in body.achunks()], [b"cached text"])

        async with await backend.send(self.request(body=BodyMode.SPOOL)) as spooled:
            self.assertIsInstance(spooled.body, SpooledBody)
            self.assertEqual(spooled.body_bytes(), b"cached text")

    def test_deferred_hit_comes_in_the_requested_body_mode(self) -> None:
        """
        Test the same on the Twisted wrapper.
        """
        backend = CachingDeferredBackend(DeferredStubBackend(Response(200, body=b"cached")))
        deferred_result(backend.send(self.request()))
        with deferred_result(backend.send(self.request(body=BodyMode.SPOOL))) as spooled:
            self.assertIsInstance(spooled.body, SpooledBody)
            self.assertEqual(spooled.body_bytes(), b"cached")


class CacheablePostTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for an operation that declares its POST cacheable — in every
    execution model: same body is a hit, another body is not
    """

    BASE_URL = "https://api.example.com"

    @staticmethod
    def answer(request: Request) -> Response:
        """Echo the request body, so each body has its own response."""
        return Response(200, body=request.body_bytes())

    def test_sync(self) -> None:
        """
        Test the cacheable POST on a synchronous backend, including the
        operation's own ttl reaching the store.
        """
        store = RecordingStore()
        inner = StubBackend(self.answer)
        client = APIClient(CachingSyncBackend(inner, store=store), self.BASE_URL)

        self.assertEqual(client.send(Translate(text="hello")), {"text": "hello"})
        self.assertEqual(client.send(Translate(text="hello")), {"text": "hello"})
        self.assertEqual(client.send(Translate(text="world")), {"text": "world"})
        self.assertEqual(len(inner.requests), 2)
        self.assertEqual(set(store.ttls.values()), {3600})

    async def test_async(self) -> None:
        """
        Test the cacheable POST on an async backend.
        """
        inner = AsyncStubBackend(self.answer)
        client = APIClient(CachingAsyncBackend(inner), self.BASE_URL)

        self.assertEqual(await client.send(Translate(text="hello")), {"text": "hello"})
        self.assertEqual(await client.send(Translate(text="hello")), {"text": "hello"})
        self.assertEqual(await client.send(Translate(text="world")), {"text": "world"})
        self.assertEqual(len(inner.requests), 2)

    def test_deferred(self) -> None:
        """
        Test the cacheable POST on a Twisted backend.
        """
        inner = DeferredStubBackend(self.answer)
        client = APIClient(CachingDeferredBackend(inner), self.BASE_URL)

        self.assertEqual(deferred_result(client.send(Translate(text="hello"))), {"text": "hello"})
        self.assertEqual(deferred_result(client.send(Translate(text="hello"))), {"text": "hello"})
        self.assertEqual(deferred_result(client.send(Translate(text="world"))), {"text": "world"})
        self.assertEqual(len(inner.requests), 2)

    def test_futures(self) -> None:
        """
        Test the cacheable POST through the thread-pool backend.
        """
        inner = StubBackend(self.answer)
        with ThreadPoolBackend(CachingSyncBackend(inner)) as backend:
            client = APIClient(backend, self.BASE_URL)
            for text in ("hello", "hello", "world"):
                self.assertEqual(
                    client.send(Translate(text=text)).result(timeout=5), {"text": text}
                )
        self.assertEqual(len(inner.requests), 2)

    def test_plain_post_is_still_not_cached(self) -> None:
        """
        Test that nothing changed for a POST that does not declare
        itself cacheable.
        """

        class Order(JsonOperation[Any]):
            method = Method.POST
            path = "/orders"
            item: str = json_field()

        inner = StubBackend(self.answer)
        client = APIClient(CachingSyncBackend(inner), self.BASE_URL)
        client.send(Order(item="a"))
        client.send(Order(item="a"))
        self.assertEqual(len(inner.requests), 2)
