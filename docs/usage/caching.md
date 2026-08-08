# Caching

For read-mostly APIs there is an explicit, TTL-based response cache —
again one wrapper per execution model
({py:class}`~action0.client.caching.CachingSyncBackend` /
{py:class}`~action0.client.caching.CachingAsyncBackend` /
{py:class}`~action0.client.caching.CachingDeferredBackend`):

```python
from action0.client import APIClient
from action0.client import CachePolicy
from action0.client import CachingSyncBackend
from action0.client.backends.requests import RequestsBackend

with RequestsBackend() as inner:
    backend = CachingSyncBackend(inner, CachePolicy(ttl=60))
    client = APIClient(backend, "https://api.example.com/v1")
    first = client.send(GetRates(currency="eur"))  # network
    second = client.send(GetRates(currency="eur"))  # cache, network untouched
```

The {py:class}`~action0.client.caching.CachePolicy` decides what is
cached: GET/HEAD only, status 200 only, for `ttl` seconds, keyed by
method + URL + the `vary_headers` request headers (`Accept`,
`Accept-Language` by default). Hits are independent copies, so mutating
a served response cannot corrupt the cache; responses with streaming
bodies are never stored. Entries live in a
{py:class}`~action0.client.caching.CacheStore` — the bundled
{py:class}`~action0.client.caching.MemoryCache` is a thread-safe
in-process LRU; implement the two-method protocol to plug in memcached,
redis and friends.

On {py:class}`~action0.client.caching.CachingAsyncBackend` the store
may also be an {py:class}`~action0.client.caching.AsyncCacheStore` —
the same two methods, awaitable — so a store doing network I/O of its
own does not block the event loop. A redis-backed store is a page of
code:

```python
import pickle

from redis.asyncio import Redis

from action0.req import Response


class RedisCache:
    """An AsyncCacheStore over redis.asyncio."""

    def __init__(self, redis: Redis, prefix: str = "action0:") -> None:
        self._redis = redis
        self._prefix = prefix

    async def get(self, key: str) -> Response | None:
        data = await self._redis.get(self._prefix + key)
        return pickle.loads(data) if data is not None else None

    async def set(self, key: str, response: Response, ttl: float) -> None:
        # redis expiries are integer seconds; round up so entries never
        # outlive the policy's ttl by rounding *down* to 0
        await self._redis.set(self._prefix + key, pickle.dumps(response), ex=max(1, int(ttl)))


backend = CachingAsyncBackend(inner, store=RedisCache(Redis()))
```

(Only pickle data you trust — here it is your own cache. The sync and
Twisted wrappers take plain `CacheStore`s only.)

This is deliberately **not** an RFC 9111 HTTP cache — no `Cache-Control`
parsing, no revalidation. It is the "a result up to a minute old is
fine" cache that read-heavy API clients end up hand-rolling, made
explicit.
