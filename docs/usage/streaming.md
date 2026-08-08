# Streaming responses

By default every backend preloads the response body into memory before
`send()` returns — the right thing for API payloads. For large downloads
and endless feeds pass `stream=True` to
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
async ones.

## Downloading to a file

On a sync backend, iterate the producer's
{py:meth}`~action0.req.body.BodyProducer.chunks` — each chunk is written
out as it arrives, so the download never occupies more memory than one
chunk:

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

## Async streams

On the async backends the producer is consumed with `async for` over
{py:meth}`~action0.req.body.BodyProducer.achunks` (the sync accessors
`chunks()` / `body_bytes()` / `body_str()` raise `RuntimeError` there —
an async source has no synchronous view):

```python
import asyncio

from action0.client import Client
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.req import Request


async def download(url: str, path: str) -> None:
    async with AsyncHttpxBackend(stream=True) as backend:
        response = await Client(backend).send(Request(url))
        with open(path, "wb") as file:
            async for chunk in response.body_producer().achunks():
                file.write(chunk)
```

Chunk boundaries are whatever the transport delivers — they carry no
meaning. A consumer of a line-oriented feed (NDJSON, SSE and friends)
buffers across chunks and splits on its own delimiter:

```python
import json
from typing import Any
from typing import AsyncIterator

from action0.req.body import BodyProducer


async def json_lines(producer: BodyProducer) -> AsyncIterator[Any]:
    """Decode an NDJSON stream, one object per line."""
    buffer = b""
    async for chunk in producer.achunks():
        buffer += chunk
        while (newline := buffer.find(b"\n")) >= 0:
            line, buffer = buffer[:newline], buffer[newline + 1 :]
            if line.strip():
                yield json.loads(line)


async def watch_events(url: str) -> None:
    async with AsyncHttpxBackend(stream=True) as backend:
        response = await Client(backend).send(Request(url))
        async for event in json_lines(response.body_producer()):
            print(event["type"])
```

## Streaming through typed operations

Streaming composes with {doc}`operations <operations>` too: declare the
result type as {py:class}`~action0.req.body.BodyProducer` and have
`load()` hand out the producer instead of parsing the body. `check()`
only inspects the status, so for a streamed response the whole
`parse()` step runs at headers arrival — nothing reads the body until
your code does:

```python
from action0.client import APIClient
from action0.client import Operation
from action0.client import path_param
from action0.client.backends.requests import RequestsBackend
from action0.req import BodyProducer
from action0.req import BytesBody
from action0.req import Method
from action0.req import Response


class DownloadExport(Operation[BodyProducer]):
    method = Method.GET
    path = "/exports/{export_id}"

    export_id: int = path_param()

    def load(self, response: Response) -> BodyProducer:
        # an empty (bodyless) response streams as zero chunks
        return response.body_producer() or BytesBody(b"")


with RequestsBackend(stream=True) as backend:
    client = APIClient(backend, "https://api.example.com/v1")
    producer = client.send(DownloadExport(export_id=7))  # typed BodyProducer
    with open("export-7.csv", "wb") as file:
        for chunk in producer.chunks():
            file.write(chunk)
```

Don't combine `stream=True` with operations that parse the whole
payload: {py:class}`~action0.client.JsonOperation`'s `load()` reads the
complete body (joining the chunks), so nothing is gained — and on an
async backend that synchronous read raises `RuntimeError`. Keep bulk
endpoints on a streaming backend and JSON endpoints on a preloading one;
backends are cheap to have two of.

## Testing streaming consumers

The {doc}`stub backends <testing>` need no special support: a canned
{py:class}`~action0.req.Response` carries a streamed body by wrapping
the chunks in an {py:class}`~action0.req.body.IterableBody` (or an
{py:class}`~action0.req.body.AsyncIterableBody` for async consumers):

```python
from action0.client import Client
from action0.client.testing import StubBackend
from action0.req import IterableBody
from action0.req import Request
from action0.req import Response

backend = StubBackend(Response(200, body=IterableBody([b"alpha\n", b"beta\n"])))
response = Client(backend).send(Request("https://api.example.com/feed"))
for chunk in response.body_producer().chunks():
    print(chunk)
# b'alpha\n'
# b'beta\n'
```

## What to know

- The connection is held until the body is consumed; an abandoned
  producer closes it when garbage-collected. Consume (or drop) bodies
  promptly — and consume them fully where you can: a body read to the
  end keeps the connection alive for reuse where the underlying library
  supports it.
- Hooks fire at headers arrival — the `elapsed` an
  {py:meth}`~action0.client.hooks.Hook.on_response` sees excludes the
  body transfer.
- `body_bytes()` / `body_str()` still work on a *sync* streamed response
  (they join the chunks — once; a second read finds the producer
  drained). An async streamed body can only be read via `achunks()`.
- How timeouts guard the stream follows the underlying library:
  requests, httpx and urllib apply the timeout to each socket read, so
  it stays in force between chunks; on
  {py:class}`~action0.client.backends.aiohttp.AiohttpBackend` the
  `timeout=` budget is aiohttp's *total* and spans the body consumption
  too — for long-lived streams pass a session with a tailored
  `ClientTimeout` (e.g. `sock_read` instead of `total`).
- {doc}`Error translation <errors>` wraps `send()`, which returned at
  headers arrival — an error while iterating a streamed body (a dropped
  connection, a read timeout) is raised by the producer as the
  underlying library's own exception, not a
  {py:class}`~action0.client.errors.TransportError`.
- The {doc}`caching wrappers <caching>` never store streamed bodies, and
  a {doc}`retry wrapper <retries>` that throws away a transient streamed
  response leaves the connection cleanup to garbage collection.
- {py:class}`~action0.client.backends.twisted.TwistedBackend` has no
  streaming mode — its `readBody` collects the whole body; use a custom
  `IProtocol` consumer directly on the Agent if you need that on Twisted.
