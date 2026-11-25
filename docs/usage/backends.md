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
{py:class}`~action0.client.backends.httpx2.Httpx2Backend` and
{py:class}`~action0.client.backends.httpx2.AsyncHttpx2Backend` (the same
for the httpx2 fork, see below),
{py:class}`~action0.client.backends.aiohttp.AiohttpBackend`
(`aiohttp.ClientSession`),
{py:class}`~action0.client.backends.urllib.UrllibBackend` (a stdlib
`urllib.request` opener — zero dependencies, for simple needs),
{py:class}`~action0.client.backends.urllib3.Urllib3Backend` (a
`urllib3.PoolManager`, for projects on urllib3 without requests on top;
also takes a `retries=` policy) and
{py:class}`~action0.client.backends.twisted.TwistedBackend`
(`twisted.web.client.Agent`, plus a `reactor=` for the timeout clock).
All of them accept `timeout=`, `follow_redirects=` and `hooks=`.

## httpx or httpx2

[httpx2](https://pypi.org/project/httpx2/) is a maintained fork of httpx
0.28: the same API under the module name `httpx2`. The backends for the
two libraries share one implementation, take the same options and behave
the same — so an application switches libraries by constructing the
other backend, and nothing else changes:

```python
from action0.client.backends.httpx import HttpxBackend
from action0.client.backends.httpx2 import Httpx2Backend

backend = Httpx2Backend(timeout=10.0)  # was: HttpxBackend(timeout=10.0)
```

Each backend takes and returns the client classes of its own library
(`backend.client` is an `httpx2.Client` there, and the type checkers know
it); a client of the other library is refused. Each also recognises only
its own library's exceptions when translating errors.

Two differences come from the libraries themselves:

- **TLS trust.** httpx2 verifies server certificates against the
  operating system's trust store, httpx against the `certifi` bundle.
  The backends do not change either. A private CA installed
  system-wide is therefore trusted by the httpx2 backends only; pass a
  client created with `verify=` to pin it yourself.
- **`httpx2.alias_httpx()`** makes `import httpx` load httpx2 for the
  whole process. Both backend modules work with and without it. Call it
  before anything imports `httpx` — including
  `action0.client.backends.httpx` — or httpx2 refuses.

## Async backends and event loops

The clients of the async HTTP libraries keep connections that are bound
to the event loop they were opened on. A backend object that lives
longer than one loop therefore cannot hold a single client — which is
exactly the situation of a module-level API client in an application
that runs `asyncio.run()` per call, or uses `async_to_sync`-style
bridges from worker threads that each run their own loop.

{py:class}`~action0.client.backends.httpx.AsyncHttpxBackend`,
{py:class}`~action0.client.backends.httpx2.AsyncHttpx2Backend` and
{py:class}`~action0.client.backends.aiohttp.AiohttpBackend` handle that:
created without a client, they keep **one client per event loop**. It is
created on first use in a loop and closed when that loop shuts down
(`asyncio.run()` and `asyncio.Runner` do the necessary
`shutdown_asyncgens()`), so one backend can be shared freely:

```python
import asyncio

from action0.client import APIClient
from action0.client.backends.httpx import AsyncHttpxBackend

client = APIClient(AsyncHttpxBackend(), "https://api.example.com/v1")  # module level


def fetch_item(item_id: int) -> Item:  # called from sync code, any thread
    return asyncio.run(client.send(GetItem(item_id=item_id)))
```

To configure those clients, pass a factory in place of a client:

```python
import httpx

backend = AsyncHttpxBackend(
    client_factory=lambda: httpx.AsyncClient(http2=True, limits=httpx.Limits(max_connections=20))
)
```

(`session_factory=` on the aiohttp backend.) The rules:

- `aclose()` — and `async with backend:` — closes the client of the
  loop it is called in. Clients of other loops close with their loops.
- A single client passed as `client=` / `session=` stays yours and is
  bound to the loop of its first use. Using that backend from another
  loop raises a `RuntimeError` that says so and points to the factory,
  instead of the library's own "Event loop is closed".
- A loop that is closed without `shutdown_asyncgens()` (a bare
  `loop.close()`) leaves its client to the garbage collector.
- Under trio there is no asyncio loop to key on; the backend keeps one
  client, as before.

The price of a fresh loop per call is a fresh connection per call:
connections cannot outlive their loop. Where throughput matters, keep
one loop running (a background thread with `run_coroutine_threadsafe`,
or async code all the way) and the backend will reuse its connections.

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

## Streamed and spooled response bodies

By default every backend preloads the response body into memory. For
large downloads (or endless feeds) a request can ask for a streamed body
— `send()` then returns as soon as the headers arrived, and the body is
consumed chunk by chunk over the still-open connection — or for a
spooled one, which is written to a temporary file while it arrives.
`stream=True` on a backend makes streaming its default. The body modes
have {doc}`a page of their own <streaming>`.

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
