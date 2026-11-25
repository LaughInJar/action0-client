# Response bodies: preloaded, streamed or spooled

How the body of a response is handed over is chosen per request. There
are three modes ({py:class}`~action0.client.options.BodyMode`):

| Mode | `send()` returns | `response.body` is | The connection is released |
|---|---|---|---|
| `PRELOAD` (the default) | when the body has arrived | `bytes` | before `send()` returns |
| `STREAM` | when status and headers have arrived | a single-use producer reading from the connection | when the body is consumed or the response closed |
| `SPOOL` | when the body has arrived | a {py:class}`~action0.req.body.SpooledBody`: in memory up to a threshold, in a temporary file beyond it | before `send()` returns |

Preloading is right for API payloads. Streaming is for bodies you want
to look at before deciding what to do with them, for downloads written
straight to a file and for endless feeds. Spooling is for large bodies
that are processed after the download: the memory use stays bounded, the
connection is free again, and the body can be read any number of times —
also as a seekable file object.

## Choosing the mode

A request asks for a mode with
{py:class}`~action0.client.options.RequestOptions`:

```python
from action0.client import BodyMode
from action0.client import Client
from action0.client import RequestOptions
from action0.client.backends.requests import RequestsBackend
from action0.req import Request

with RequestsBackend() as backend:
    request = Request("https://example.com/big.bin")
    RequestOptions(body=BodyMode.STREAM).apply(request)
    with Client(backend).send(request) as response:
        print(response.status, response.headers.get("Content-Length"))  # before the body
        response.save_body("big.bin")
```

An {doc}`operation <operations>` declares it once for all its requests:

```python
class DownloadExport(Operation[BodyProducer]):
    path = "/exports/{export_id}"
    request_options = RequestOptions(body=BodyMode.STREAM)
    ...
```

Every backend that does its own I/O supports all three modes — except
{py:class}`~action0.client.backends.twisted.TwistedBackend`, which
preloads and spools but cannot stream (see *What to know* below).
The backends' `stream=True` constructor argument makes `STREAM` the
default of that backend; a request's own option still wins.

## Closing responses

A streamed response holds its connection and a spooled one may hold a
temporary file, so close them when you are done: use the response as a
context manager (`with` on sync backends, `async with` on async ones) or
call `close()` / `aclose()`. Reading a streamed body to its end releases
the connection as well. Closing is a no-op for preloaded bodies, so
`with client.send(request) as response:` is always safe. Garbage
collection cleans up too, but only as a last resort.

## Streaming

On a sync backend the body is an
{py:class}`~action0.req.body.IterableBody`, consumed through
{py:meth}`~action0.req.body.BodyProducer.chunks`. To write it to a file,
{py:meth}`~action0.req.response.Response.save_body` does the loop — the
download never occupies more memory than one chunk.

On the async backends the body is an
{py:class}`~action0.req.body.AsyncIterableBody`, consumed with
`async for` over {py:meth}`~action0.req.body.BodyProducer.achunks` (the
sync accessors `chunks()` / `body_bytes()` / `body_str()` raise
`RuntimeError` there — an async source has no synchronous view).
{py:meth}`~action0.req.response.Response.asave_body` writes it to a file
without blocking the event loop: the file operations run in a worker
thread.

```python
from action0.client import BodyMode
from action0.client import Client
from action0.client import RequestOptions
from action0.client.backends.httpx import AsyncHttpxBackend
from action0.req import Request


async def download(url: str, path: str) -> None:
    async with AsyncHttpxBackend() as backend:
        request = RequestOptions(body=BodyMode.STREAM).apply(Request(url))
        async with await Client(backend).send(request) as response:
            if response.is_success:
                await response.asave_body(path)
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
    async with AsyncHttpxBackend(stream=True) as backend:  # streaming as the default
        async with await Client(backend).send(Request(url)) as response:
            async for event in json_lines(response.body_producer()):
                print(event["type"])
```

### Resuming a download

Because a streamed response is in your hands before its body is read,
you decide what reaches the file. A download that is continued with an
HTTP `Range` request appends only when the server really answered with
the requested part — an error page never ends up in the file. And if the
connection breaks midway, `save_body()` has written what arrived, so the
size of the file is the offset to continue from:

```python
import os

from action0.client import BodyMode
from action0.client import Client
from action0.client import RequestOptions
from action0.client import TransportError
from action0.req import Request
from action0.req import Response


def download(client: Client[Response], url: str, path: str, attempts: int = 5) -> None:
    """Download a URL into a file, resuming after connection failures."""
    for _ in range(attempts):
        offset = os.path.getsize(path) if os.path.exists(path) else 0
        request = Request(url)
        RequestOptions(body=BodyMode.STREAM).apply(request)
        if offset:
            request.headers["Range"] = f"bytes={offset}-"
        try:
            with client.send(request) as response:
                content_range = response.headers.get("Content-Range", "")
                if response.status == 206 and content_range.startswith(f"bytes {offset}-"):
                    response.save_body(path, append=True)  # the requested rest
                elif response.status == 200:
                    response.save_body(path)  # the whole file: start over
                else:
                    raise RuntimeError(f"cannot download: {response.status} {response.phrase}")
            return
        except TransportError:
            continue  # the connection broke; what arrived is in the file
    raise RuntimeError(f"giving up after {attempts} attempts")
```

How much of the data that preceded a break reaches the file depends on
the HTTP library: most hand out everything they received before they
raise, while aiohttp drops what it had buffered but not yet handed out.
The loop above is correct either way, because it takes the offset from
the file.

A break in the middle of a body is raised as the library-independent
{py:class}`~action0.client.errors.TransportError` (or
{py:class}`~action0.client.errors.TimeoutError`) on every backend, with
the HTTP library's own exception as `__cause__` — so code like this does
not depend on the backend in use.

## Spooling

With `BodyMode.SPOOL` the backend writes the body into a
{py:class}`~action0.req.body.SpooledBody` while it arrives.
`spool_max_memory` bytes (1 MiB unless the request says otherwise) are
kept in memory; a larger body moves to an anonymous temporary file (in
`spool_dir`, by default the system's temporary directory). When `send()`
returns, the download is complete and the connection released.

The spooled body is re-readable, and
{py:meth}`~action0.req.response.Response.body_file` returns it as a
seekable binary file object — what streaming parsers and decompressors
take. An operation for a large gzipped report can hand out that file:

```python
import gzip
import json
from typing import IO

from action0.client import APIClient
from action0.client import BodyMode
from action0.client import Operation
from action0.client import RequestOptions
from action0.client import path_param
from action0.client.backends.requests import RequestsBackend
from action0.req import Response


class DownloadReport(Operation[IO[bytes]]):
    path = "/reports/{name}.json.gz"
    request_options = RequestOptions(body=BodyMode.SPOOL, spool_max_memory=8 * 2**20)

    name: str = path_param()

    def load(self, response: Response) -> IO[bytes]:
        file = response.body_file()
        assert file is not None
        return file


with RequestsBackend() as backend:
    client = APIClient(backend, "https://api.example.com/v1")
    with client.send(DownloadReport(name="daily")) as file:  # closing it removes the temp file
        with gzip.GzipFile(fileobj=file) as report:
            rows = json.load(report)["rows"]
```

The file object is the spool's own file, so closing it discards the
data; closing the response does the same. An incremental JSON parser
such as `ijson` reads from the same file object without loading the
document into memory.

### Spooling without blocking the event loop

On the async backends the spool is filled without stalling the loop:

- While the body fits in memory, chunks are appended inline — a memory
  copy, no system call.
- The write that moves the body to the temporary file, and every write
  after it, runs in a worker thread: `asyncio.to_thread` on asyncio, the
  trio thread pool when running below trio. The backends ask their
  library for 64 KiB chunks, so a 300 MB download costs about 4,600
  thread hops.
- {py:class}`~action0.client.backends.twisted.TwistedBackend` writes in
  the reactor's thread pool and pauses the transport while a write is
  running; what still arrives meanwhile goes into the next write.

Reading a spooled body in async code follows the same rule:
`achunks()` and `asave_body()` use worker threads once the data is on
disk. A synchronous parser working on `body_file()` should itself run
in a thread (`await asyncio.to_thread(parse, response.body_file())`).

## Streaming through typed operations

Streaming composes with {doc}`operations <operations>` too: declare the
result type as {py:class}`~action0.req.body.BodyProducer` and have
`load()` hand out the producer instead of parsing the body. `check()`
only inspects the status, so for a streamed response the whole
`parse()` step runs at headers arrival — nothing reads the body until
your code does:

```python
from action0.client import APIClient
from action0.client import BodyMode
from action0.client import Operation
from action0.client import RequestOptions
from action0.client import path_param
from action0.client.backends.requests import RequestsBackend
from action0.req import BodyProducer
from action0.req import BytesBody
from action0.req import Response
from action0.req import save_body


class DownloadExport(Operation[BodyProducer]):
    path = "/exports/{export_id}"
    request_options = RequestOptions(body=BodyMode.STREAM)

    export_id: int = path_param()

    def load(self, response: Response) -> BodyProducer:
        # an empty (bodyless) response streams as zero chunks
        return response.body_producer() or BytesBody(b"")


with RequestsBackend() as backend:
    client = APIClient(backend, "https://api.example.com/v1")
    producer = client.send(DownloadExport(export_id=7))  # typed BodyProducer
    save_body(producer, "export-7.csv")
```

Because the mode belongs to the operation, the same client and backend
serve streamed downloads and ordinary JSON endpoints side by side. Don't
ask for a streamed body in an operation that parses the whole payload:
{py:class}`~action0.client.JsonOperation`'s `load()` reads the complete
body, so nothing is gained — and on an async backend that synchronous
read raises `RuntimeError`.

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

- A streamed body holds its connection until it is consumed or the
  response is closed. Consume bodies fully where you can: a body read
  to the end keeps the connection alive for reuse where the underlying
  library supports it.
- Hooks fire when `send()` returns. For a streamed response that is at
  headers arrival — the `elapsed` an
  {py:meth}`~action0.client.hooks.Hook.on_response` sees excludes the
  body transfer. For preloaded and spooled responses it includes it.
- `body_bytes()` / `body_str()` still work on a *sync* streamed response
  (they join the chunks — once; a second read finds the producer
  drained). An async streamed body can only be read via `achunks()`. A
  spooled body can be read any number of times, in both ways.
- How timeouts guard a stream follows the underlying library: requests,
  httpx and urllib apply the timeout to each socket read, so it stays in
  force between chunks; on
  {py:class}`~action0.client.backends.aiohttp.AiohttpBackend` the
  `timeout=` budget is aiohttp's *total* and spans the body consumption
  too — for long-lived streams pass a session with a tailored
  `ClientTimeout` (e.g. `sock_read` instead of `total`).
- {doc}`Error translation <errors>` covers the body: an error while a
  streamed body is read (a dropped connection, a read timeout) is raised
  by the producer as a {py:class}`~action0.client.errors.TransportError`
  or {py:class}`~action0.client.errors.TimeoutError`, like an error
  raised by `send()`. An error while a body is spooled is raised by
  `send()` itself, and the half-filled spool is discarded.
- A body that ends before its announced `Content-Length` is an error in
  every mode and on every backend. (The standard library's `urllib`
  does not report this by itself when a body is read in chunks;
  {py:class}`~action0.client.backends.urllib.UrllibBackend` checks it.)
- The {doc}`caching wrappers <caching>` never store streamed or spooled
  bodies, and a {doc}`retry wrapper <retries>` that throws away a
  transient streamed response leaves the connection cleanup to garbage
  collection.
- {py:class}`~action0.client.backends.twisted.TwistedBackend` preloads
  and spools, but has no streamed mode: a request asking for
  `BodyMode.STREAM` fails with a `ValueError` before anything is sent.
  Streaming on Twisted is planned, see
  [TODO.md](https://github.com/LaughInJar/action0-client/blob/main/TODO.md).
- {py:class}`~action0.client.backends.futures.ThreadPoolBackend` passes
  requests on to the backend it wraps, so the modes work there
  unchanged.
