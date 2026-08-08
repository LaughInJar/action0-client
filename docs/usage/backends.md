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
