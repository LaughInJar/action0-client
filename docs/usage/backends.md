# Backends and their configuration

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

## Parallel requests from sync code

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

## Streaming response bodies

By default every backend preloads the response body into memory. For
large downloads (or endless feeds) pass `stream=True` to
{py:class}`~action0.client.backends.requests.RequestsBackend`,
{py:class}`~action0.client.backends.httpx.HttpxBackend`,
{py:class}`~action0.client.backends.httpx.AsyncHttpxBackend`,
{py:class}`~action0.client.backends.aiohttp.AiohttpBackend`,
{py:class}`~action0.client.backends.urllib.UrllibBackend` or
{py:class}`~action0.client.backends.urllib3.Urllib3Backend`: `send()`
then returns as soon as the headers arrived, and the response body is a
streaming {py:class}`~action0.req.body.BodyProducer` over the still-open
connection — an {py:class}`~action0.req.body.IterableBody` on the sync
backends, an {py:class}`~action0.req.body.AsyncIterableBody` on the
async ones:

```python
from action0.client import Client
from action0.client.backends.requests import RequestsBackend
from action0.req import Request

with RequestsBackend(stream=True) as backend:
    response = Client(backend).send(Request("https://example.com/big.bin"))
    producer = response.body_producer()
    with open("big.bin", "wb") as file:
        for chunk in producer.chunks():
            file.write(chunk)
```

On the async backends, iterate `async for chunk in producer.achunks()`
instead. What to know:

- The connection is held until the body is consumed; an abandoned
  producer closes it when garbage-collected. Consume (or drop) bodies
  promptly.
- Hooks fire at headers arrival — the `elapsed` an
  {py:meth}`~action0.client.hooks.Hook.on_response` sees excludes the
  body transfer.
- `body_bytes()` / `body_str()` still work on a streamed response (they
  join the chunks — once); an *async* streamed body can only be read via
  `achunks()`, the sync accessors raise `RuntimeError`.
- The caching wrappers never store streamed bodies, and a retry wrapper
  that throws away a transient streamed response leaves the connection
  cleanup to garbage collection.
- {py:class}`~action0.client.backends.twisted.TwistedBackend` has no
  streaming mode — its `readBody` collects the whole body; use a custom
  `IProtocol` consumer directly on the Agent if you need that on Twisted.

## Greenlet stacks (gevent, eventlet)

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
