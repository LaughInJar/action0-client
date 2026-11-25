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
two-method protocol. Store calls are synchronous and expected to be fast
— except on :py:class:`CachingAsyncBackend`, which also accepts an
:py:class:`AsyncCacheStore` (awaitable ``get``/``set``) for stores that
do network I/O of their own, like redis or memcached.
"""

import hashlib
import inspect
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Awaitable
from typing import Callable
from typing import Protocol

from action0.req import Request
from action0.req import Response

from .backend import Backend
from .backends._body import adeliver_bytes
from .backends._body import deliver_bytes
from .options import RequestOptions
from .wrapper import AsyncBackendWrapper
from .wrapper import DeferredBackendWrapper
from .wrapper import SyncBackendWrapper

if TYPE_CHECKING:
    # twisted is an optional dependency: only the type checker sees this
    from twisted.internet.defer import Deferred


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


class AsyncCacheStore(Protocol):
    """
    The awaitable flavor of :py:class:`CacheStore`, for stores that do
    network I/O of their own — redis, memcached and friends, driven by
    their asyncio clients. Accepted by :py:class:`CachingAsyncBackend`
    only: the sync and Twisted wrappers have no natural place to await.
    """

    def get(self, key: str) -> "Awaitable[Response | None]":
        """
        Look up a cached response.

        :param key: the cache key
        :return: (an awaitable of) the cached response, or ``None`` for
                 a miss (including expired entries)
        """
        ...

    def set(self, key: str, response: Response, ttl: float) -> Awaitable[None]:
        """
        Store a response.

        :param key: the cache key
        :param response: the response to store
        :param ttl: the seconds the entry may be served
        :return: an awaitable completing once stored
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
    German and an English representation of the same URL don't collide).
    These are the ONLY headers in the key: volatile ones — a rotating
    bearer token, a signature, a timestamp — never fragment the cache.
    The flip side: if the response depends on who is asking, name the
    header that says so here (or give such requests an explicit
    :py:attr:`~action0.client.options.RequestOptions.cache_key`)."""

    max_body_size: "int | None" = None
    """The largest response body, in bytes, that is stored; larger ones
    are passed through uncached. ``None`` sets no limit — reasonable for
    the in-process :py:class:`MemoryCache`, worth setting for a cache
    server with a value size limit."""

    def key_for(self, request: Request) -> str:
        """
        The cache key of a request: the request's own
        :py:attr:`~action0.client.options.RequestOptions.cache_key` if it
        has one, otherwise a digest of method, full URL, the
        :py:attr:`vary_headers` values and — for a request that has one —
        the body.

        :param request: the request to key
        :return: the explicit key, or a digest string
        """
        explicit = RequestOptions.of(request).cache_key
        if explicit is not None:
            return explicit
        parts = [request.method, request.url.as_str()]
        for name in self.vary_headers:
            parts.append(f"{name.casefold()}: {','.join(request.headers.get_all(name))}")
        if isinstance(request.body, (bytes, str)):
            # only requests with a body get this part, so the keys of
            # bodyless requests are what they always were
            body = request.body_bytes() or b""
            parts.append(f"body: {hashlib.sha256(body).hexdigest()}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def should_lookup(self, request: Request) -> bool:
        """
        Whether the cache applies to this request at all. The request
        can say so itself
        (:py:attr:`RequestOptions.cacheable
        <action0.client.options.RequestOptions.cacheable>`); otherwise
        its method decides (see :py:attr:`methods`).

        Fail-safe: a request whose body is a streaming producer cannot
        be keyed by its body, so it is never cached under a computed key
        — only under an explicit one.

        :param request: the request about to be sent
        :return: whether to consult (and later fill) the cache
        """
        options = RequestOptions.of(request)
        allowed = options.cacheable
        if allowed is None:
            allowed = request.method in self.methods
        if not allowed:
            return False
        hashable = request.body is None or isinstance(request.body, (bytes, str))
        return hashable or options.cache_key is not None

    def ttl_for(self, request: Request) -> float:
        """
        How long the response to a request may be served from the cache.

        :param request: the request that was sent
        :return: the request's own
                 :py:attr:`~action0.client.options.RequestOptions.cache_ttl`,
                 otherwise :py:attr:`ttl`
        """
        own = RequestOptions.of(request).cache_ttl
        return self.ttl if own is None else own

    def should_store(self, request: Request, response: Response) -> bool:
        """
        Whether a fresh response should be put into the cache. Responses
        whose body is a producer (streamed or spooled) are never stored,
        nor are bodies larger than :py:attr:`max_body_size`.

        :param request: the request that was sent
        :param response: the response that arrived
        :return: whether to store it
        """
        if not self.should_lookup(request) or response.status not in self.statuses:
            return False
        if response.body is None:
            return True
        if not isinstance(response.body, (bytes, str)):
            return False
        if self.max_body_size is None:
            return True
        return len(response.body_bytes() or b"") <= self.max_body_size


def _entry(response: Response) -> Response:
    """
    What goes into the store for a response: an independent copy without
    the request reference. The request is not needed to serve a hit, and
    it would carry credentials (an ``Authorization`` header, a password
    in the URL) and the request's ``meta`` into the store — possibly a
    cache server shared with others.

    :param response: the response that arrived
    :return: the copy to store
    """
    return response.copy(request=None)


def _fresh_copy(cached: Response, request: Request) -> Response:
    """
    The response handed out for a cache hit: an independent copy (so
    callers mutating it cannot corrupt the cache) tied to the *current*
    request, with the body in the form that request asked for.

    :param cached: the stored response
    :param request: the request being answered from the cache
    :return: the copy to hand out
    """
    return cached.copy(request=request, body=deliver_bytes(request, cached))


async def _afresh_copy(cached: Response, request: Request) -> Response:
    """
    :py:func:`_fresh_copy` for the async wrapper (a body the request
    wants streamed or spooled is built the async way).

    :param cached: the stored response
    :param request: the request being answered from the cache
    :return: the copy to hand out
    """
    return cached.copy(request=request, body=await adeliver_bytes(request, cached))


class CachingSyncBackend(SyncBackendWrapper):
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
        super().__init__(inner)
        self._policy = policy
        self._store = store if store is not None else MemoryCache()

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
            self._store.set(key, _entry(response), self._policy.ttl_for(request))
        return response


class CachingAsyncBackend(AsyncBackendWrapper):
    """
    A caching wrapper around an async backend — itself a
    ``Backend[Awaitable[Response]]``, so it plugs into the clients like
    the backend it wraps. Takes either store flavor: a plain
    :py:class:`CacheStore` is called synchronously from inside the
    coroutine (keep it fast — the bundled :py:class:`MemoryCache` is),
    an :py:class:`AsyncCacheStore` is awaited, so it may do network I/O
    of its own (redis, memcached, ...).
    """

    def __init__(
        self,
        inner: Backend[Awaitable[Response]],
        policy: CachePolicy = CachePolicy(),
        store: "CacheStore | AsyncCacheStore | None" = None,
    ) -> None:
        """
        :param inner: the backend that actually sends
        :param policy: what to cache and for how long
        :param store: where entries live, sync or async; ``None`` creates
                      a :py:class:`MemoryCache`
        """
        super().__init__(inner)
        self._policy = policy
        self._store: "CacheStore | AsyncCacheStore" = store if store is not None else MemoryCache()

    @property
    def store(self) -> "CacheStore | AsyncCacheStore":
        """The cache store (e.g. for clearing it)."""
        return self._store

    async def _lookup(self, key: str) -> "Response | None":
        """
        Look up an entry in whichever store flavor is plugged in.

        :param key: the cache key
        :return: the cached response, or ``None`` for a miss
        """
        found = self._store.get(key)
        if inspect.isawaitable(found):
            # an AsyncCacheStore — isawaitable is a TypeIs, so the sync
            # flavor is narrowed out in the else path (ty loses the await
            # result type here; mypy would flag a cast as redundant)
            return await found  # ty: ignore[invalid-return-type]
        return found

    async def _keep(self, key: str, response: Response, ttl: float) -> None:
        """
        Store an entry in whichever store flavor is plugged in.

        :param key: the cache key
        :param response: the (already copied) response to store
        :param ttl: the seconds the entry may be served
        """
        stored = self._store.set(key, response, ttl)
        if inspect.isawaitable(stored):
            await stored

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
        cached = await self._lookup(key)
        if cached is not None:
            return await _afresh_copy(cached, request)
        response = await self._inner.send(request)
        if self._policy.should_store(request, response):
            await self._keep(key, _entry(response), self._policy.ttl_for(request))
        return response


class CachingDeferredBackend(DeferredBackendWrapper):
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
        super().__init__(inner)
        self._policy = policy
        self._store = store if store is not None else MemoryCache()

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
                self._store.set(key, _entry(response), self._policy.ttl_for(request))
            return response

        return self._inner.send(request).addCallback(store_and_pass)
