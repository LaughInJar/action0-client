# Guide

The examples that show outputs (in `#` comments) are runnable as-is — they
use the stub backends from {py:mod}`action0.client.testing` instead of the
network, exactly like your tests would.

## Installation

`action0-client` is not published to PyPI yet; install it straight from
GitHub. The HTTP libraries are optional — pick the extras matching the
backend(s) you want:

```shell
uv add "action0-client[httpx] @ git+https://github.com/LaughInJar/action0-client"

# extras: requests, httpx, aiohttp, urllib3, twisted, all
pip install "action0-client[requests,twisted] @ git+https://github.com/LaughInJar/action0-client"
```

Without extras you get the core library — clients, operations, test stubs
— plus two stdlib-only backends that need no extra at all:
{py:class}`~action0.client.backends.urllib.UrllibBackend` (basic sync
HTTP via `urllib.request`) and
{py:class}`~action0.client.backends.futures.ThreadPoolBackend` (parallel
sends over a thread pool). For real workloads pick one of the library
backends.

## The pieces

- {py:class}`~action0.req.request.Request` /
  {py:class}`~action0.req.response.Response` (from
  [action0-req](https://laughinjar.github.io/action0-req/)) describe
  *what* is on the wire.
- A **backend** performs the HTTP I/O — through whichever library and
  execution model you chose. Backends implement one structural protocol,
  {py:class}`~action0.client.backend.Backend`, generic over what `send()`
  wraps the response in (`Backend[Response]` is a sync backend,
  `Backend[Awaitable[Response]]` an asyncio one, ...); the built-in ones
  live in {py:mod}`action0.client.backends`.
- {py:class}`~action0.client.client.Client` sends raw requests through a
  backend ("ring 0").
- {py:class}`~action0.client.operation.Operation` +
  {py:class}`~action0.client.api.APIClient` describe *an API*: typed
  endpoint classes in, typed results out ("ring 1").

The key idea: **the backend decides the execution model, and the types
follow it.** With a sync backend `send()` returns the result, with an
async backend an `Awaitable`, with a Twisted backend a `Deferred` — the
same client and operation classes throughout, verified by mypy, pyright
and ty.

## Raw requests: Client

Synchronously, with the [requests](https://requests.readthedocs.io/)
backend:

```python
from action0.client import Client
from action0.client.backends.requests import RequestsBackend
from action0.req import Request

with RequestsBackend() as backend:
    client = Client(backend)
    response = client.send(Request("https://example.com/"))  # Response
    print(response.status, response.headers.get("Content-Type"))
```

On asyncio, with the [httpx](https://www.python-httpx.org/) backend — the
only change is the backend (and the `await`):

```python
import asyncio
from action0.client import Client
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.req import Request


async def main() -> None:
    async with AsyncHttpxBackend() as backend:
        client = Client(backend)
        response = await client.send(Request("https://example.com/"))
        print(response.status)


asyncio.run(main())
```

On [Twisted](https://twisted.org/), `send()` returns a
`Deferred[Response]`:

```python
from twisted.internet import reactor
from action0.client import Client
from action0.client.backends.twisted import TwistedBackend
from action0.req import Request

client = Client(TwistedBackend())
deferred = client.send(Request("https://example.com/"))
deferred.addCallback(lambda response: print(response.status))
deferred.addBoth(lambda _: reactor.stop())
reactor.run()
```

There is also a sync httpx backend
({py:class}`~action0.client.backends.httpx.HttpxBackend`) and an
[aiohttp](https://docs.aiohttp.org/) backend
({py:class}`~action0.client.backends.aiohttp.AiohttpBackend`) that drops
in exactly like the async httpx one — use whichever library your project
already depends on.

Async here does not mean asyncio-only: this library's async machinery
uses nothing but `async`/`await`, and httpx does its I/O through
[anyio](https://anyio.readthedocs.io/) — so
{py:class}`~action0.client.backends.httpx.AsyncHttpxBackend` (and the
async test stub) run under [trio](https://trio.readthedocs.io/)
unchanged, `trio.run` instead of `asyncio.run`; this is pinned by the
test suite. `AiohttpBackend` is asyncio-only, as aiohttp itself is.

Runnable stub version (this is what the type checker sees, too — `send()`
returns a plain {py:class}`~action0.req.response.Response` because the
backend is synchronous):

```python
from action0.client import Client
from action0.client.testing import StubBackend
from action0.req import Request, Response

client = Client(StubBackend(Response(204)))
print(client.send(Request("https://api.example.com/ping")).status)
# 204
```

## Backends and their configuration

Every built-in backend takes its library's native client/session/agent, so
nothing of the underlying library is hidden from you:

```python
import httpx
from action0.client.backends.httpx import HttpxBackend

# bring your own client (pooling, HTTP/2, proxies, ...) — it stays yours
# and is not closed by the backend
backend = HttpxBackend(httpx.Client(http2=True))

# or let the backend create one; then the backend owns and closes it
backend = HttpxBackend(timeout=10.0, follow_redirects=False)
```

The counterparts:
{py:class}`~action0.client.backends.requests.RequestsBackend`
(`requests.Session`),
{py:class}`~action0.client.backends.httpx.AsyncHttpxBackend`
(`httpx.AsyncClient`, `async with` / `aclose()`),
{py:class}`~action0.client.backends.aiohttp.AiohttpBackend`
(`aiohttp.ClientSession`, created lazily on the first send when none is
passed),
{py:class}`~action0.client.backends.urllib.UrllibBackend` (a stdlib
`urllib.request` opener — zero dependencies, for simple needs),
{py:class}`~action0.client.backends.urllib3.Urllib3Backend` (a
`urllib3.PoolManager`, for projects on urllib3 without requests on top;
also takes a `retries=` policy) and
{py:class}`~action0.client.backends.twisted.TwistedBackend`
(`twisted.web.client.Agent`, plus a `reactor=` for the timeout clock).
All of them accept `timeout=`, `follow_redirects=` and `hooks=`.

### Parallel requests from sync code

{py:class}`~action0.client.backends.futures.ThreadPoolBackend` (stdlib,
no extra) wraps any synchronous backend and runs its sends on a
`ThreadPoolExecutor` — its execution model is
`concurrent.futures.Future`, and the types follow, including through
`APIClient`:

```python
from action0.client import APIClient
from action0.client.backends.futures import ThreadPoolBackend
from action0.client.backends.requests import RequestsBackend

with RequestsBackend() as inner, ThreadPoolBackend(inner) as backend:
    client = APIClient(backend, "https://api.example.com/v1")
    futures = [client.send(GetItem(item_id=item_id)) for item_id in range(100)]
    items = [future.result() for future in futures]  # each one a Future[Item]
```

Hooks belong on the *wrapped* backend (they run on the pool threads,
around the actual I/O); the wrapper itself stays out of the way.

### Greenlet stacks (gevent, eventlet)

Nothing extra is needed for [gevent](https://www.gevent.org/) or
eventlet: monkey-patching turns the blocking sockets under
{py:class}`~action0.client.backends.requests.RequestsBackend` and
{py:class}`~action0.client.backends.urllib.UrllibBackend` cooperative,
exactly as it does for plain `requests`/`urllib` code. From this
library's point of view those stacks are simply synchronous — `send()`
returns the plain `Response`, typed by the sync overloads; the yielding
to other greenlets happens inside the socket layer. (Don't combine
monkey-patching with the asyncio, trio or Twisted backends in the same
process — that caveat comes from the greenlet libraries, not from here.)

## Instrumentation hooks

Backends built on the base classes run
{py:class}`~action0.client.hooks.Hook`s around every send — the same hook
API in all three execution models (hooks are plain synchronous calls that
run around the I/O, never inside it):

```python
import logging
import sys
from action0.client import LoggingHook
from action0.client.testing import StubBackend
from action0.req import Request, Response

logger = logging.getLogger("docs.hooks")
logger.propagate = False
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.StreamHandler(sys.stdout))

backend = StubBackend(Response(200), hooks=[LoggingHook(logger)])
backend.send(Request("https://api.example.com/health"))
# -> Request(GET https://api.example.com/health)
# <- Response(200 OK) for Request(GET https://api.example.com/health) in 0ms
```

`repr()` of requests and responses redacts secret header values and URL
passwords, which makes {py:class}`~action0.client.hooks.LoggingHook` safe
for production logs.

Custom hooks subclass {py:class}`~action0.client.hooks.Hook` and override
what they need — `on_request` (may replace the request), `on_response`
(may replace the response, gets the elapsed seconds) and `on_error`:

```python
from action0.client import Hook
from action0.req import Request, Response


class MetricsHook(Hook):
    """Collect response counts and timings per status."""

    def __init__(self) -> None:
        self.timings: dict[int, list[float]] = {}

    def on_response(self, request: Request, response: Response, elapsed: float) -> None:
        self.timings.setdefault(response.status, []).append(elapsed)


class DefaultUserAgentHook(Hook):
    """Stamp a User-Agent onto requests that have none."""

    def on_request(self, request: Request) -> Request | None:
        request.headers.setdefault("User-Agent", "my-service/1.0")
        return request
```

Hooks can be passed to any built-in backend (`hooks=[...]`) or appended
later (`backend.hooks.append(...)`).

## Error handling

Backends translate their library's exceptions into one family, so calling
code never depends on the HTTP library:

- {py:class}`~action0.client.errors.ClientError` — everything below
- {py:class}`~action0.client.errors.TransportError` — no usable HTTP
  response (DNS, connect, TLS, connection lost); the original library
  exception stays chained as `__cause__`
- {py:class}`~action0.client.errors.TimeoutError` — a `TransportError`
  that is also the built-in `TimeoutError`
- {py:class}`~action0.client.errors.APIError` — an HTTP response arrived
  but was unusable (unexpected status, malformed payload); carries
  `.request` and `.response`

```python
from action0.client import APIError, TransportError

try:
    item = client.send(GetItem(item_id=42))
except TransportError as error:
    ...  # network trouble — retry, circuit-break, ...
except APIError as error:
    ...  # the API answered, but not with what we expected
```

With async and Twisted backends the same errors arrive at `await` time /
in the errback instead of being raised synchronously.

## Retries

Transient failures are common enough to deserve a battery: wrap any
backend in the retrying variant of its execution model
({py:class}`~action0.client.retry.RetryingSyncBackend` /
{py:class}`~action0.client.retry.RetryingAsyncBackend` /
{py:class}`~action0.client.retry.RetryingDeferredBackend`) and failed
sends are repeated with exponential backoff. The wrapper preserves the
execution model — the clients (and the type checker) treat it exactly
like the backend it wraps — and the wrapped backend's hooks run on every
attempt, so logs and metrics see the retries:

```python
from action0.client import APIClient, RetryPolicy, RetryingSyncBackend
from action0.client.backends.requests import RequestsBackend

with RequestsBackend() as inner:
    backend = RetryingSyncBackend(inner, RetryPolicy(attempts=5, backoff=0.2))
    client = APIClient(backend, "https://api.example.com/v1")
    item = client.send(GetItem(item_id=42))  # still typed Item
```

What is retried is the {py:class}`~action0.client.retry.RetryPolicy`'s
call — by default: `TransportError`s (which the backends translate all
network failures into) and the transient statuses 408/429/500/502/503/504,
for idempotent methods only (`methods=None` lifts that gate). When the
attempt budget is exhausted, the last response is returned (or the last
error raised) unchanged — the policy never invents failures.

How long is waited is also the policy's call: exponential backoff with
"full jitter" — each wait is a uniformly random duration up to the
exponential delay, so a fleet of clients hitting the same outage does
not retry in lockstep (`jitter=False` waits the exact delays). A
`Retry-After` response header (seconds or HTTP-date form) overrides the
computed wait — the server knows best — capped at the policy's
`max_backoff`; `respect_retry_after=False` ignores it.

Two execution-model notes: the async wrapper waits with `asyncio.sleep`
by default — under trio pass `sleep=trio.sleep`; the Twisted wrapper
takes a `reactor=` for its backoff timer (the global reactor by
default).

## Caching

For read-mostly APIs there is an explicit, TTL-based response cache —
again one wrapper per execution model
({py:class}`~action0.client.caching.CachingSyncBackend` /
{py:class}`~action0.client.caching.CachingAsyncBackend` /
{py:class}`~action0.client.caching.CachingDeferredBackend`):

```python
from action0.client import APIClient, CachePolicy, CachingSyncBackend
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

## Typed API clients: operations

An {py:class}`~action0.client.operation.Operation` describes one endpoint.
The constant parts (HTTP method, path template) are class attributes; the
variable parts are typed dataclass fields, placed into the request by the
specifiers from {py:mod}`action0.client.fields`; the generic parameter is
the parsed result type:

```python
from dataclasses import dataclass
from typing import Any
from action0.client import JsonOperation, path_param, query
from action0.req import Method


@dataclass
class Item:
    id: int
    name: str


class GetItem(JsonOperation[Item]):
    method = Method.GET
    path = "/items/{item_id}"

    item_id: int = path_param()  # fills the {item_id} placeholder
    expand: bool | None = query(default=None)  # ?expand=...; None = not sent

    def load_json(self, data: Any) -> Item:
        return Item(id=data["id"], name=data["name"])
```

Subclasses become keyword-only dataclasses automatically (no decorator
needed), so `GetItem(item_id=42)` is type-checked, has a useful `repr()`
and compares by value. The class definition is validated eagerly: a path
placeholder without a matching `path_param()` field (or vice versa), a
field named like a class attribute, or conflicting body declarations all
raise `TypeError` at import time.

{py:class}`~action0.client.api.APIClient` binds the backend, the base URL
and default headers, and `send()` runs the full pipeline — build the
request, send it, `parse()` the response:

```python
from action0.client import APIClient
from action0.client.testing import StubBackend
from action0.req import Response

backend = StubBackend(Response(200, body='{"id": 42, "name": "Thing"}'))
client = APIClient(backend, "https://api.example.com/v1")

print(client.send(GetItem(item_id=42, expand=True)))
# Item(id=42, name='Thing')
print(backend.requests[0].url.as_str())
# https://api.example.com/v1/items/42?expand=true
print(backend.requests[0].headers["Accept"])
# application/json
```

And, the whole point: with an async or Twisted backend **the same
operations** yield `Awaitable[Item]` / `Deferred[Item]`:

```python
from action0.client.testing import AsyncStubBackend, DeferredStubBackend

async_client = APIClient(AsyncStubBackend(...), "https://api.example.com/v1")
item = await async_client.send(GetItem(item_id=42))  # Awaitable[Item]

twisted_client = APIClient(DeferredStubBackend(...), "https://api.example.com/v1")
deferred = twisted_client.send(GetItem(item_id=42))  # Deferred[Item]
deferred.addCallback(...)
```

### Field placement

```python
from action0.client import (
    JsonOperation,
    body,
    form_field,
    header,
    json_body,
    json_field,
    path_param,
    query,
)
from action0.req import Method
from typing import Any


class CreateItem(JsonOperation[Any]):
    method = Method.POST
    path = "/shelves/{shelf}/items"

    shelf: str = path_param()  # path template placeholder
    dry_run: bool | None = query("dryRun", default=None)  # renamed on the wire
    locale: str | None = header("Accept-Language", default=None)
    token: str = header("X-API-Key", repr=False)  # kept out of repr()

    name: str = json_field()  # keys of the JSON body object
    tags: list[str] | None = json_field(default=None)
```

- All `json_field()`s together form the JSON object body (with
  `Content-Type: application/json` added if unset).
- `json_body()` sends one field — scalar, mapping, sequence, dataclass —
  as the entire JSON body instead.
- All `form_field()`s together form an
  `application/x-www-form-urlencoded` body — the classic HTML form POST
  and the shape of OAuth token endpoints. Values serialize exactly like
  query parameters:

  ```python
  class RequestToken(JsonOperation[Any]):
      method = Method.POST
      path = "/oauth/token"

      grant_type: str = form_field(default="client_credentials")
      client_id: str = form_field()
      client_secret: str = form_field(repr=False)


  # body: grant_type=client_credentials&client_id=...&client_secret=...
  # Content-Type: application/x-www-form-urlencoded
  ```

- `body()` sends one field as the raw body: `bytes`, `str` or a streaming
  {py:class}`~action0.req.body.BodyProducer`.
- Only one of these body forms per operation (several `json_field()`s
  *or* several `form_field()`s *or* a single `json_body()` / `body()`),
  checked at class-creation time.

Serialization is uniform and overridable: `None` means "not sent" (except
for path parameters, which must not be `None`), enums send their
``value``, dates/datetimes their ISO form, booleans the web-style
``true``/``false``, and list values repeat the query parameter or header.
A `serialize=` callable on any field overrides the value's serialization;
`serialize_value` / `serialize_json_value` on the operation override it
family-wide.

Fields *without* a specifier follow the operation's `default_location` —
query parameters by default. An API family whose endpoints all POST JSON
can flip that once in a base class:

```python
from typing import Any, ClassVar
from action0.client import JsonOperation
from action0.client.fields import Location
from action0.req import Method


class RpcOperation(JsonOperation[Any]):
    """Every endpoint of this API takes a JSON object body."""

    method = Method.POST
    default_location: ClassVar[Location] = Location.JSON_FIELD


class SearchProducts(RpcOperation):
    path = "/search_product"
    query: str  # plain fields → JSON body keys, thanks to the base
    limit: int = 30
```

### Response handling

`parse()` = `check()` + `load()`:

- `check()` raises {py:class}`~action0.client.errors.APIError` for
  anything but 2xx — override it for per-endpoint status policies (e.g.
  tolerate 404 and return `None`).
- `load()` turns the vetted response into the result.
  {py:class}`~action0.client.operation.JsonOperation` implements it by
  decoding JSON and delegating to `load_json(data)`; override `load_json`
  for typed models (the default returns the decoded payload as-is, which
  fits `JsonOperation[Any]`).

For non-JSON endpoints subclass
{py:class}`~action0.client.operation.Operation` directly and implement
`load()` — see `DeletePet` in
[examples/petstore.py](https://github.com/LaughInJar/action0-client/blob/main/examples/petstore.py)
for a 204-no-body endpoint.

### A real client class

Applications usually wrap `APIClient` once per API, fixing base URL and
auth. Keep the backend type variable, so the typed overloads keep working
for every execution model:

```python
from action0.client import APIClient, BackendT_co


class PetStoreClient(APIClient[BackendT_co]):
    def __init__(self, backend: BackendT_co, token: str) -> None:
        super().__init__(
            backend,
            "https://petstore.example.com/v1",
            headers={"Authorization": f"Bearer {token}"},
        )
```

Default headers fill gaps only — a header the operation sets itself wins.
For dynamic per-request work (signing, token refresh) override
{py:meth}`~action0.client.api.APIClient.prepare`.

The complete, runnable version of this client — models, operations,
client, all three execution models and the stub-backed demo — lives in
[examples/petstore.py](https://github.com/LaughInJar/action0-client/blob/main/examples/petstore.py).

## Testing API clients

{py:mod}`action0.client.testing` ships one stub backend per execution
model: canned (or computed) responses in, requests recorded, hooks and
error translation running like on real backends —
{py:class}`~action0.client.testing.StubBackend`,
{py:class}`~action0.client.testing.AsyncStubBackend`,
{py:class}`~action0.client.testing.DeferredStubBackend`:

```python
from action0.client.testing import StubBackend, deferred_result
from action0.req import Request, Response

backend = StubBackend(Response(200), Response(503))  # in order, last repeats
client = PetStoreClient(backend, token="test")

client.send(SomeOperation(...))
assert backend.requests[0].url.path == "/v1/some/path"
```

A callable responder covers dynamic cases including failures:

```python
def flaky(request: Request) -> Response:
    raise ConnectionResetError("nope")


backend = StubBackend(flaky)
```

{py:func}`~action0.client.testing.deferred_result` extracts the result of
an already-fired `Deferred` (stubs fire synchronously), so Twisted code
paths test without a reactor.

## Writing your own backend

A backend is anything implementing the
{py:class}`~action0.client.backend.Backend` protocol — two methods, no
registration:

- `send(request)` — do the I/O, return the (wrapped)
  {py:class}`~action0.req.response.Response`.
- `map(result, fn)` — apply `fn` inside the wrapper: plain call / await /
  `addCallback`. This is how generic code (like
  {py:meth}`APIClient.send <action0.client.api.APIClient.send>`) attaches
  response parsing without knowing your execution model.

In practice, subclass the matching base class and implement only the raw
I/O — hooks, error translation and `map` come for free:

```python
from action0.client import BaseSyncBackend, TransportError
from action0.req import Request, Response
import urllib.request, urllib.error


class UrllibBackend(BaseSyncBackend):
    """A tiny stdlib-only backend."""

    def _send(self, request: Request) -> Response:
        raw = urllib.request.Request(
            request.url.as_str(),
            method=request.method,
            headers=dict(request.headers.items()),
            data=request.body_bytes(),
        )
        with urllib.request.urlopen(raw) as answer:
            return Response(
                answer.status,
                headers=answer.getheaders(),
                body=answer.read(),
                request=request,
            )

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        if isinstance(error, urllib.error.URLError):
            return TransportError(str(error.reason), request=request)
        return error
```

`Client(UrllibBackend())` and `APIClient(UrllibBackend(), ...)` now work,
fully typed, hooks included. The async and Deferred base classes
({py:class}`~action0.client.backend.BaseAsyncBackend`,
{py:class}`~action0.client.backend.BaseDeferredBackend`) mirror this with
an `async def _send` / a Deferred-returning `_send`.

### Other execution models

The protocol is generic over the wrapper type, so backends are not limited
to the shipped execution models (sync, awaitable, Deferred, Future). Say
your framework has a result wrapper of its own:

```python
from typing import Callable, Generic, TypeVar
from action0.client import Client
from action0.req import Request, Response

T = TypeVar("T")
S = TypeVar("S")


class Box(Generic[T]):
    """Stand-in for your framework's own result wrapper."""


class BoxBackend:
    def send(self, request: Request) -> Box[Response]: ...
    def map(self, result: Box[T], fn: Callable[[T], S]) -> Box[S]: ...
```

`Client(BoxBackend()).send(request)` is a `Box[Response]` — the return
type is derived from the backend, no client code involved. For
{py:meth}`APIClient.send <action0.client.api.APIClient.send>` the parsed
results of such a backend are typed `Any` (rewriting "the wrapper, around
the operation's result type" for an *arbitrary* wrapper would need
higher-kinded types, which Python doesn't have — only the shipped
wrappers are spelled out as overloads); everything works normally at
runtime.

If you want that precision for your wrapper too, the escape hatch is to
subclass {py:class}`~action0.client.api.APIClient` and re-declare `send()`
with the wrapper spelled out — one `cast` is the price of the missing
higher-kinded types:

```python
from typing import TypeVar, cast
from action0.client import APIClient, Backend, Operation
from action0.req import Response

R = TypeVar("R")
BoxBackendT_co = TypeVar("BoxBackendT_co", bound=Backend[Box[Response]], covariant=True)


class BoxAPIClient(APIClient[BoxBackendT_co]):
    """An APIClient whose send() results are precisely typed Boxes."""

    # pyright ignore: its override check compares against every parent
    # overload without filtering by this subclass's self type (none of
    # the shipped-wrapper overloads can ever apply to a Box backend)
    def send(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, operation: Operation[R]
    ) -> Box[R]:
        return cast(Box[R], super().send(operation))
```

Now `BoxAPIClient(BoxBackend(), "https://api.example.com")` sends an
`Operation[Item]` as a `Box[Item]` — for every operation, without
per-call casts, and the concrete backend type stays visible on
`client.backend`. mypy and ty accept the override as-is; pyright wants the
one suppression shown above. The pattern is pinned in the typing test
suite (`tests/action0/client/test_typing.py`), so it keeps working.
