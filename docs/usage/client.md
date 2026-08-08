# Raw requests: Client

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
from action0.req import Request
from action0.req import Response

client = Client(StubBackend(Response(204)))
print(client.send(Request("https://api.example.com/ping")).status)
# 204
```
