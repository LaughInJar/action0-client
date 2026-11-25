import asyncio
import unittest
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from twisted.internet.defer import Deferred
from twisted.internet.defer import succeed

from action0.client import APIClient
from action0.client import AsyncBackendWrapper
from action0.client import Backend
from action0.client import Client
from action0.client import DeferredBackendWrapper
from action0.client import Hook
from action0.client import JsonOperation
from action0.client import RetryPolicy
from action0.client import SyncBackendWrapper
from action0.client.caching import CachingAsyncBackend
from action0.client.caching import CachingDeferredBackend
from action0.client.caching import CachingSyncBackend
from action0.client.retry import RetryingAsyncBackend
from action0.client.retry import RetryingDeferredBackend
from action0.client.retry import RetryingSyncBackend
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend
from action0.client.testing import StubBackend
from action0.client.testing import deferred_result
from action0.req import Request
from action0.req import Response

URL = "https://api.example.com/ping"


class Ping(JsonOperation[Any]):
    """The example operation used in the client integration tests."""

    path = "/ping"


class CountingHook(Hook):
    """A metrics-style hook counting the sends that reach its backend."""

    def __init__(self) -> None:
        self.sends = 0

    def on_request(self, request: Request) -> None:
        self.sends += 1


class SyncThrottle(SyncBackendWrapper):
    """A wrapper with a (recorded) blocking step before every send."""

    def __init__(self, inner: Backend[Response], log: list[str]) -> None:
        super().__init__(inner)
        self._log = log

    def send(self, request: Request) -> Response:
        self._log.append("waited")
        return self.inner.send(request)


class AsyncThrottle(AsyncBackendWrapper):
    """A wrapper awaiting a rate limiter before every send."""

    def __init__(self, inner: Backend[Awaitable[Response]], log: list[str]) -> None:
        super().__init__(inner)
        self._log = log

    async def _acquire(self) -> None:
        # a real suspension point, like a limiter that has to wait
        await asyncio.sleep(0)
        self._log.append("acquired")

    async def send(self, request: Request) -> Response:
        await self._acquire()
        return await self.inner.send(request)


class DeferredThrottle(DeferredBackendWrapper):
    """A wrapper waiting for a Deferred before every send."""

    def __init__(self, inner: "Backend[Deferred[Response]]", log: list[str]) -> None:
        super().__init__(inner)
        self._log = log

    def send(self, request: Request) -> "Deferred[Response]":
        def acquired(_: None) -> "Deferred[Response]":
            self._log.append("acquired")
            return self.inner.send(request)

        waiting: "Deferred[None]" = succeed(None)
        return waiting.addCallback(acquired)


class _OddMapBackend:
    """
    A sync backend whose ``map`` is recognizably its own — to observe
    that wrappers delegate ``map`` instead of reimplementing it.
    """

    def __init__(self) -> None:
        self.mapped = 0

    def send(self, request: Request) -> Response:
        return Response(200, body="{}")

    def map(self, result: Any, fn: Callable[[Any], Any]) -> Any:
        self.mapped += 1
        return fn(result)


class SyncBackendWrapperTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.wrapper.SyncBackendWrapper`
    """

    def test_passes_through_by_default(self) -> None:
        """
        Test that the base class alone is a transparent wrapper: send,
        inner and repr.
        """
        inner = StubBackend(Response(204))
        backend = SyncBackendWrapper(inner)
        self.assertEqual(backend.send(Request(URL)).status, 204)
        self.assertIs(backend.inner, inner)
        self.assertEqual(repr(backend), "SyncBackendWrapper(StubBackend(1 requests))")

    def test_subclass_runs_its_step_before_every_send(self) -> None:
        """
        Test the intended use: an overridden send doing its part and then
        delegating; the repr names the subclass.
        """
        log: list[str] = []
        inner = StubBackend(Response(200, body='{"pong": true}'))
        backend = SyncThrottle(inner, log)

        client = APIClient(backend, "https://api.example.com")
        self.assertEqual(client.send(Ping()), {"pong": True})
        self.assertEqual(Client(backend).send(Request(URL)).status, 200)
        self.assertEqual(log, ["waited", "waited"])
        self.assertEqual(repr(backend), "SyncThrottle(StubBackend(2 requests))")

    def test_map_is_delegated(self) -> None:
        """
        Test that map() goes to the wrapped backend, also through nested
        wrappers.
        """
        inner = _OddMapBackend()
        backend = SyncBackendWrapper(SyncBackendWrapper(inner))
        self.assertEqual(backend.map(20, lambda value: value + 1), 21)
        self.assertEqual(inner.mapped, 1)

    def test_library_wrappers_are_built_on_it(self) -> None:
        """
        Test that the retry and caching wrappers share the base class and
        with it inner, map and repr.
        """
        inner = StubBackend()
        for backend in (RetryingSyncBackend(inner), CachingSyncBackend(inner)):
            with self.subTest(backend=backend):
                self.assertIsInstance(backend, SyncBackendWrapper)
                self.assertIs(backend.inner, inner)
                self.assertEqual(backend.map("a", str.upper), "A")
                self.assertEqual(repr(backend), f"{type(backend).__name__}({inner!r})")


class AsyncBackendWrapperTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests for :py:class:`action0.client.wrapper.AsyncBackendWrapper`
    """

    async def test_passes_through_by_default(self) -> None:
        """
        Test that the base class alone is a transparent wrapper.
        """
        inner = AsyncStubBackend(Response(204))
        backend = AsyncBackendWrapper(inner)
        self.assertEqual((await backend.send(Request(URL))).status, 204)
        self.assertIs(backend.inner, inner)
        self.assertEqual(repr(backend), "AsyncBackendWrapper(AsyncStubBackend(1 requests))")

    async def test_subclass_awaits_before_every_send(self) -> None:
        """
        Test what hooks cannot do: an awaited step before each request,
        finished before the request reaches the wrapped backend.
        """
        log: list[str] = []

        def answer(request: Request) -> Response:
            log.append("sent")
            return Response(200, body='{"pong": true}')

        backend = AsyncThrottle(AsyncStubBackend(answer), log)
        client = APIClient(backend, "https://api.example.com")
        self.assertEqual(await client.send(Ping()), {"pong": True})
        self.assertEqual((await Client(backend).send(Request(URL))).status, 200)
        self.assertEqual(log, ["acquired", "sent", "acquired", "sent"])

    async def test_map_is_delegated(self) -> None:
        """
        Test that map() applies the function inside the awaitable.
        """
        backend = AsyncBackendWrapper(AsyncStubBackend())

        async def value() -> int:
            return 20

        self.assertEqual(await backend.map(value(), lambda number: number + 1), 21)

    async def test_library_wrappers_are_built_on_it(self) -> None:
        """
        Test that the retry and caching wrappers share the base class.
        """
        inner = AsyncStubBackend()
        for backend in (RetryingAsyncBackend(inner), CachingAsyncBackend(inner)):
            with self.subTest(backend=backend):
                self.assertIsInstance(backend, AsyncBackendWrapper)
                self.assertIs(backend.inner, inner)
                self.assertEqual(repr(backend), f"{type(backend).__name__}({inner!r})")


class DeferredBackendWrapperTestCase(unittest.TestCase):
    """
    tests for :py:class:`action0.client.wrapper.DeferredBackendWrapper`
    """

    def test_passes_through_by_default(self) -> None:
        """
        Test that the base class alone is a transparent wrapper.
        """
        inner = DeferredStubBackend(Response(204))
        backend = DeferredBackendWrapper(inner)
        self.assertEqual(deferred_result(backend.send(Request(URL))).status, 204)
        self.assertIs(backend.inner, inner)
        self.assertEqual(repr(backend), "DeferredBackendWrapper(DeferredStubBackend(1 requests))")

    def test_subclass_waits_before_every_send(self) -> None:
        """
        Test a Deferred-returning step chained in front of the send, used
        through both clients.
        """
        log: list[str] = []
        inner = DeferredStubBackend(Response(200, body='{"pong": true}'))
        backend = DeferredThrottle(inner, log)

        client = APIClient(backend, "https://api.example.com")
        self.assertEqual(deferred_result(client.send(Ping())), {"pong": True})
        self.assertEqual(deferred_result(Client(backend).send(Request(URL))).status, 200)
        self.assertEqual(log, ["acquired", "acquired"])

    def test_map_is_delegated(self) -> None:
        """
        Test that map() applies the function inside the Deferred.
        """
        backend = DeferredBackendWrapper(DeferredStubBackend())
        fired: "Deferred[int]" = succeed(20)
        self.assertEqual(deferred_result(backend.map(fired, lambda number: number + 1)), 21)

    def test_library_wrappers_are_built_on_it(self) -> None:
        """
        Test that the retry and caching wrappers share the base class.
        """
        inner = DeferredStubBackend()
        for backend in (RetryingDeferredBackend(inner), CachingDeferredBackend(inner)):
            with self.subTest(backend=backend):
                self.assertIsInstance(backend, DeferredBackendWrapper)
                self.assertIs(backend.inner, inner)
                self.assertEqual(repr(backend), f"{type(backend).__name__}({inner!r})")


class StackingOrderTestCase(unittest.IsolatedAsyncioTestCase):
    """
    tests pinning the documented stacking order of cache, retry and
    throttle wrappers around a backend with hooks
    """

    def setUp(self) -> None:
        self.log: list[str] = []
        self.hook = CountingHook()
        self.policy = RetryPolicy(attempts=3, backoff=0)

    def test_hook_on_the_inner_backend_sees_every_attempt(self) -> None:
        """
        Test that a hook on the wrapped backend counts each attempt of a
        retried request, not just the request.
        """
        inner = StubBackend(Response(503), Response(503), Response(200), hooks=[self.hook])
        backend = RetryingSyncBackend(inner, self.policy)
        self.assertEqual(backend.send(Request(URL)).status, 200)
        self.assertEqual(self.hook.sends, 3)

    def test_cache_hit_never_reaches_the_inner_backend(self) -> None:
        """
        Test that a cache hit involves neither the wrapped backend's
        hooks nor the wrappers below the cache.
        """
        inner = StubBackend(Response(200, body="fetched"), hooks=[self.hook])
        backend = CachingSyncBackend(SyncThrottle(inner, self.log))
        for _ in range(3):
            self.assertEqual(backend.send(Request(URL)).body_str(), "fetched")
        self.assertEqual(self.hook.sends, 1)
        self.assertEqual(self.log, ["waited"])

    async def test_recommended_async_stack(self) -> None:
        """
        Test the documented order cache → retry → throttle → backend: the
        throttle is passed before every attempt, the cached answer of the
        second request passes nothing at all.
        """
        inner = AsyncStubBackend(Response(503), Response(200, body="fetched"), hooks=[self.hook])
        backend = CachingAsyncBackend(
            RetryingAsyncBackend(AsyncThrottle(inner, self.log), self.policy)
        )
        self.assertEqual((await backend.send(Request(URL))).body_str(), "fetched")
        self.assertEqual(self.log, ["acquired", "acquired"])
        self.assertEqual(self.hook.sends, 2)

        self.assertEqual((await backend.send(Request(URL))).body_str(), "fetched")
        self.assertEqual(self.log, ["acquired", "acquired"])
        self.assertEqual(self.hook.sends, 2)

    def test_throttle_outside_the_retry_is_passed_once(self) -> None:
        """
        Test the other order: a throttle wrapped around the retry counts
        requests, not attempts.
        """
        inner = StubBackend(Response(503), Response(200))
        backend = SyncThrottle(RetryingSyncBackend(inner, self.policy), self.log)
        self.assertEqual(backend.send(Request(URL)).status, 200)
        self.assertEqual(self.log, ["waited"])
        self.assertEqual(len(inner.requests), 2)
