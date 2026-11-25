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
cached: GET/HEAD only, status 200 only, for `ttl` seconds. Hits are
independent copies, so mutating a served response cannot corrupt the
cache. Responses with a streamed or spooled body are never stored, and
`max_body_size` keeps bodies above a size out of the cache — worth
setting for a cache server with a value size limit.

## The cache key

A response is stored under a digest of

- the method and the full URL,
- the values of the `vary_headers` request headers (`Accept` and
  `Accept-Language` by default), and
- the request body, for a request that has one.

The body is always part of the key, so two requests that differ only in
their body can never be answered with each other's response.

The `vary_headers` are the only headers in the key. Volatile headers — a
bearer token that is rotated, a signature, a timestamp — therefore never
fragment the cache. The flip side: **if the response depends on who is
asking, say so**, by naming the header that identifies the caller:

```python
from action0.client import CachePolicy

policy = CachePolicy(ttl=60, vary_headers=("Accept", "Accept-Language", "X-Tenant"))
```

## Per request: cacheable, key and lifetime

An {doc}`operation <operations>` (or a single request) can decide for
itself, with {py:class}`~action0.client.options.RequestOptions`:

- `cacheable=True` caches a request although its method is not GET or
  HEAD — for a `POST` that only reads, like a search or a translation
  endpoint. `cacheable=False` keeps a request out of the cache.
- `cache_ttl` sets the lifetime of this response instead of the
  policy's `ttl`.
- `cache_key` replaces the computed key, as given.

```python
from typing import Any

from action0.client import APIClient
from action0.client import CachingSyncBackend
from action0.client import JsonOperation
from action0.client import RequestOptions
from action0.client import json_field
from action0.client.testing import StubBackend
from action0.req import Method
from action0.req import Request
from action0.req import Response


class Translate(JsonOperation[Any]):
    method = Method.POST
    path = "/translate"
    request_options = RequestOptions(cacheable=True, cache_ttl=3600)

    text: str = json_field()


def echo(request: Request) -> Response:
    return Response(200, body=request.body_bytes())


inner = StubBackend(echo)
client = APIClient(CachingSyncBackend(inner), "https://api.example.com")
print(client.send(Translate(text="hello")))  # network
# {'text': 'hello'}
print(client.send(Translate(text="hello")))  # cache: same body, same key
# {'text': 'hello'}
print(client.send(Translate(text="world")))  # network: another body
# {'text': 'world'}
print(len(inner.requests))
# 2
```

An explicit `cache_key` is for requests the computed key does not fit:
when something volatile is part of the URL or the body (a nonce, a
timestamp), or when the request body is a stream. A streamed body cannot
be hashed, so such a request is never cached under a computed key — only
under one it brings along. Whoever sets a key is responsible for it
telling apart everything that changes the response.

A hit is handed out in the {doc}`body mode <streaming>` the request asks
for: code that expects a spooled body gets one whether or not the cache
answered.

## Cache stores

Entries live in a {py:class}`~action0.client.caching.CacheStore` — the
bundled {py:class}`~action0.client.caching.MemoryCache` is a thread-safe
in-process LRU. The protocol has two methods, so a cache server is a few
lines away. Stored responses can be pickled, and they are stored
**without their request**: neither an `Authorization` header nor a
password in the URL nor the request's `meta` reaches the store.

An adapter for any key-value client offering `get(key)` and
`set(key, value, ttl)`:

```python
import pickle
from typing import Protocol

from action0.req import Response


class KeyValueClient(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def set(self, key: str, value: bytes, ttl: int) -> None: ...


class KeyValueCache:
    """A CacheStore over a key-value client."""

    def __init__(self, client: KeyValueClient, prefix: str = "action0:") -> None:
        self._client = client
        self._prefix = prefix

    def get(self, key: str) -> Response | None:
        data = self._client.get(self._prefix + key)
        return pickle.loads(data) if data is not None else None

    def set(self, key: str, response: Response, ttl: float) -> None:
        # expiries are usually whole seconds; never round down to "no expiry"
        self._client.set(self._prefix + key, pickle.dumps(response), ttl=max(1, int(ttl)))


backend = CachingSyncBackend(inner, store=KeyValueCache(client))
```

On {py:class}`~action0.client.caching.CachingAsyncBackend` the store
may also be an {py:class}`~action0.client.caching.AsyncCacheStore` —
the same two methods, awaitable — so a store doing network I/O of its
own does not block the event loop:

```python
class AsyncKeyValueClient(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl: int) -> None: ...


class AsyncKeyValueCache:
    """An AsyncCacheStore over an async key-value client."""

    def __init__(self, client: AsyncKeyValueClient, prefix: str = "action0:") -> None:
        self._client = client
        self._prefix = prefix

    async def get(self, key: str) -> Response | None:
        data = await self._client.get(self._prefix + key)
        return pickle.loads(data) if data is not None else None

    async def set(self, key: str, response: Response, ttl: float) -> None:
        await self._client.set(self._prefix + key, pickle.dumps(response), ttl=max(1, int(ttl)))


backend = CachingAsyncBackend(inner, store=AsyncKeyValueCache(client))
```

With `redis.asyncio` the client calls are `await redis.get(key)` and
`await redis.set(key, value, ex=ttl)`.

Only unpickle data you trust — here it is your own cache. The sync and
Twisted wrappers take plain `CacheStore`s only.

This is deliberately **not** an RFC 9111 HTTP cache — no `Cache-Control`
parsing, no revalidation. It is the "a result up to a minute old is
fine" cache that read-heavy API clients end up hand-rolling, made
explicit.
