"""
Explicit, TTL-based response caching: wrap a backend in the caching
variant of its execution model and repeated safe requests are served
from the cache instead of the network.

This is deliberately **not** an RFC 9111 HTTP cache — no ``Cache-Control``
parsing, no validators or revalidation. It is an application-level cache
for read-mostly APIs, where "a result up to N seconds old is fine" is a
decision the *caller* makes via the :py:class:`CachePolicy`. Like the
retry wrappers, the caching wrappers preserve the wrapped backend's
execution model (and with it the static types); on a cache hit the
wrapped backend — and therefore its hooks — is not involved at all.

Entries are stored in a :py:class:`CacheStore` — the bundled
:py:class:`MemoryCache` is a thread-safe in-process LRU with per-entry
expiry; bring your own store (memcached, redis, ...) by implementing the
two-method protocol. Store calls are synchronous in every execution
model and are expected to be fast.
"""

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Awaitable
from typing import Callable
from typing import Protocol
from typing import TypeVar

from action0.req import Request
from action0.req import Response

from .backend import Backend

if TYPE_CHECKING:
    # twisted is an optional dependency: only the type checker sees this
    from twisted.internet.defer import Deferred

T = TypeVar("T")
S = TypeVar("S")


class CacheStore(Protocol):
    """
    Where cached responses live: any object with ``get``/``set`` — the
    bundled :py:class:`MemoryCache`, or your own adapter to memcached,
    redis and friends. Implementations own the expiry bookkeeping.
    """

    def get(self, key: str) -> "Response | None":
        """
        Look up a cached response.

        :param key: the cache key
        :return: the cached response, or ``None`` for a miss (including
                 expired entries)
        """
        ...

    def set(self, key: str, response: Response, ttl: float) -> None:
        """
        Store a response.

        :param key: the cache key
        :param response: the response to store
        :param ttl: the seconds the entry may be served
        """
        ...


class MemoryCache:
    """
    The bundled :py:class:`CacheStore`: an in-process, thread-safe LRU
    with per-entry expiry.

    Example::

        >>> cache = MemoryCache(maxsize=2)
        >>> cache.set("a", Response(200, body="cached"), ttl=60)
        >>> cache.get("a")
        Response(200 OK)
        >>> cache.get("gone") is None
        True
    """

    def __init__(self, maxsize: int = 128, *, clock: Callable[[], float] = time.monotonic) -> None:
        """
        :param maxsize: the number of entries kept; the least recently
                        used one is evicted first
        :param clock: the monotonic time source (injectable for tests)
        :raises ValueError: if maxsize is not positive
        """
        if maxsize <= 0:
            raise ValueError(f"maxsize must be positive, got {maxsize}")
        self._maxsize = maxsize
        self._clock = clock
        self._entries: "OrderedDict[str, tuple[float, Response]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> "Response | None":
        """
        Look up a response, dropping it if expired.

        :param key: the cache key
        :return: the cached response, or ``None`` for a miss
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, response = entry
            if self._clock() >= expires:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return response

    def set(self, key: str, response: Response, ttl: float) -> None:
        """
        Store a response, evicting the least recently used entries beyond
        the size limit.

        :param key: the cache key
        :param response: the response to store
        :param ttl: the seconds the entry may be served; zero or negative
                    stores nothing
        """
        if ttl <= 0:
            return
        with self._lock:
            self._entries[key] = (self._clock() + ttl, response)
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop all entries."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        """
        :return: the number of entries (including not-yet-collected
                 expired ones)
        """
        with self._lock:
            return len(self._entries)


@dataclass(frozen=True)
class CachePolicy:
    """
    What to cache, for how long, and under which key — immutable, shared
    freely between backends.
    """

    ttl: float = 300.0
    """The seconds a cached response may be served."""

    methods: frozenset[str] = frozenset({"GET", "HEAD"})
    """The methods that are cached at all; everything else always goes to
    the network."""

    statuses: frozenset[int] = frozenset({200})
    """The response statuses worth caching."""

    vary_headers: tuple[str, ...] = ("Accept", "Accept-Language")
    """The request headers that become part of the cache key (so e.g. a
    German and an English representation of the same URL don't collide)."""

    def key_for(self, request: Request) -> str:
        """
        The cache key of a request: method, full URL and the
        :py:attr:`vary_headers` values.

        :param request: the request to key
        :return: a digest string
        """
        parts = [request.method, request.url.as_str()]
        for name in self.vary_headers:
            parts.append(f"{name.casefold()}: {','.join(request.headers.get_all(name))}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def should_lookup(self, request: Request) -> bool:
        """
        Whether the cache applies to this request at all.

        :param request: the request about to be sent
        :return: whether to consult (and later fill) the cache
        """
        return request.method in self.methods

    def should_store(self, request: Request, response: Response) -> bool:
        """
        Whether a fresh response should be put into the cache. Responses
        with streaming bodies are never stored — a
        :py:class:`~action0.req.body.BodyProducer` may be single-use.

        :param request: the request that was sent
        :param response: the response that arrived
        :return: whether to store it
        """
        return (
            request.method in self.methods
            and response.status in self.statuses
            and (response.body is None or isinstance(response.body, (bytes, str)))
        )


def _fresh_copy(cached: Response, request: Request) -> Response:
    """
    The response handed out for a cache hit: an independent copy (so
    callers mutating it cannot corrupt the cache) tied to the *current*
    request.

    :param cached: the stored response
    :param request: the request being answered from the cache
    :return: the copy to hand out
    """
    return cached.copy(request=request)


class CachingSyncBackend:
    """
    A caching wrapper around a synchronous backend — itself a
    ``Backend[Response]``, so it plugs into the clients like the backend
    it wraps.

    Example::

        >>> from action0.client import Client
        >>> from action0.client.testing import StubBackend
        >>> from action0.req import Request, Response
        >>>
        >>> inner = StubBackend(Response(200, body="fetched"))
        >>> client = Client(CachingSyncBackend(inner))
        >>> client.send(Request("https://api.example.com/rates")).body_str()
        'fetched'
        >>> client.send(Request("https://api.example.com/rates")).body_str()
        'fetched'
        >>> len(inner.requests)  # the second send never hit the network
        1
    """

    def __init__(
        self,
        inner: Backend[Response],
        policy: CachePolicy = CachePolicy(),
        store: "CacheStore | None" = None,
    ) -> None:
        """
        :param inner: the backend that actually sends
        :param policy: what to cache and for how long
        :param store: where entries live; ``None`` creates a
                      :py:class:`MemoryCache`
        """
        self._inner = inner
        self._policy = policy
        self._store = store if store is not None else MemoryCache()

    @property
    def inner(self) -> Backend[Response]:
        """The wrapped backend doing the actual sends."""
        return self._inner

    @property
    def store(self) -> CacheStore:
        """The cache store (e.g. for clearing it)."""
        return self._store

    def send(self, request: Request) -> Response:
        """
        Serve from the cache when the policy allows and an entry is
        fresh; otherwise send through the wrapped backend and store a
        cacheable response.

        :param request: the request to send
        :return: the (possibly cached) response
        """
        if not self._policy.should_lookup(request):
            return self._inner.send(request)
        key = self._policy.key_for(request)
        cached = self._store.get(key)
        if cached is not None:
            return _fresh_copy(cached, request)
        response = self._inner.send(request)
        if self._policy.should_store(request, response):
            self._store.set(key, response.copy(), self._policy.ttl)
        return response

    def map(self, result: T, fn: Callable[[T], S]) -> S:
        """
        Apply a function to a result of :py:meth:`send` — synchronously
        that is simply ``fn(result)``.

        :param result: a value as returned by :py:meth:`send`
        :param fn: the function to apply
        :return: the return value of ``fn``
        """
        return fn(result)

    def __repr__(self) -> str:
        """
        :return: the wrapper with its wrapped backend
        """
        return f"{self.__class__.__name__}({self._inner!r})"


class CachingAsyncBackend:
    """
    A caching wrapper around an async backend — itself a
    ``Backend[Awaitable[Response]]``, so it plugs into the clients like
    the backend it wraps. The store is called synchronously from inside
    the coroutine; keep it fast (the bundled :py:class:`MemoryCache` is).
    """

    def __init__(
        self,
        inner: Backend[Awaitable[Response]],
        policy: CachePolicy = CachePolicy(),
        store: "CacheStore | None" = None,
    ) -> None:
        """
        :param inner: the backend that actually sends
        :param policy: what to cache and for how long
        :param store: where entries live; ``None`` creates a
                      :py:class:`MemoryCache`
        """
        self._inner = inner
        self._policy = policy
        self._store = store if store is not None else MemoryCache()

    @property
    def inner(self) -> Backend[Awaitable[Response]]:
        """The wrapped backend doing the actual sends."""
        return self._inner

    @property
    def store(self) -> CacheStore:
        """The cache store (e.g. for clearing it)."""
        return self._store

    async def send(self, request: Request) -> Response:
        """
        Serve from the cache when the policy allows and an entry is
        fresh; otherwise send through the wrapped backend and store a
        cacheable response.

        :param request: the request to send
        :return: (an awaitable of) the (possibly cached) response
        """
        if not self._policy.should_lookup(request):
            return await self._inner.send(request)
        key = self._policy.key_for(request)
        cached = self._store.get(key)
        if cached is not None:
            return _fresh_copy(cached, request)
        response = await self._inner.send(request)
        if self._policy.should_store(request, response):
            self._store.set(key, response.copy(), self._policy.ttl)
        return response

    def map(self, result: Awaitable[T], fn: Callable[[T], S]) -> Awaitable[S]:
        """
        Apply a function inside an awaitable result of :py:meth:`send`.

        :param result: an awaitable as returned by :py:meth:`send`
        :param fn: the function to apply to the awaited value
        :return: an awaitable of the return value of ``fn``
        """

        async def mapped() -> S:
            return fn(await result)

        return mapped()

    def __repr__(self) -> str:
        """
        :return: the wrapper with its wrapped backend
        """
        return f"{self.__class__.__name__}({self._inner!r})"


class CachingDeferredBackend:
    """
    A caching wrapper around a Twisted backend — itself a
    ``Backend[Deferred[Response]]``, so it plugs into the clients like
    the backend it wraps. Cache hits fire the returned Deferred
    synchronously.
    """

    def __init__(
        self,
        inner: "Backend[Deferred[Response]]",
        policy: CachePolicy = CachePolicy(),
        store: "CacheStore | None" = None,
    ) -> None:
        """
        :param inner: the backend that actually sends
        :param policy: what to cache and for how long
        :param store: where entries live; ``None`` creates a
                      :py:class:`MemoryCache`
        """
        self._inner = inner
        self._policy = policy
        self._store = store if store is not None else MemoryCache()

    @property
    def inner(self) -> "Backend[Deferred[Response]]":
        """The wrapped backend doing the actual sends."""
        return self._inner

    @property
    def store(self) -> CacheStore:
        """The cache store (e.g. for clearing it)."""
        return self._store

    def send(self, request: Request) -> "Deferred[Response]":
        """
        Serve from the cache when the policy allows and an entry is
        fresh; otherwise send through the wrapped backend and store a
        cacheable response.

        :param request: the request to send
        :return: a Deferred firing with the (possibly cached) response
        """
        from twisted.internet.defer import succeed

        if not self._policy.should_lookup(request):
            return self._inner.send(request)
        key = self._policy.key_for(request)
        cached = self._store.get(key)
        if cached is not None:
            return succeed(_fresh_copy(cached, request))

        def store_and_pass(response: Response) -> Response:
            if self._policy.should_store(request, response):
                self._store.set(key, response.copy(), self._policy.ttl)
            return response

        return self._inner.send(request).addCallback(store_and_pass)

    def map(self, result: "Deferred[T]", fn: Callable[[T], S]) -> "Deferred[S]":
        """
        Apply a function inside a Deferred result of :py:meth:`send` —
        Twisted's native ``addCallback``.

        :param result: a Deferred as returned by :py:meth:`send`
        :param fn: the function to apply to the eventual value
        :return: a Deferred firing with the return value of ``fn``
        """
        return result.addCallback(fn)

    def __repr__(self) -> str:
        """
        :return: the wrapper with its wrapped backend
        """
        return f"{self.__class__.__name__}({self._inner!r})"
