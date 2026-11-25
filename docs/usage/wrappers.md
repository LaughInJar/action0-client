# Backend wrappers

A wrapper is a backend that passes every request on to another backend
and does something of its own around that. It is the tool for what a
{py:class}`~action0.client.hooks.Hook` cannot do: hooks are plain
synchronous callbacks, so they cannot `await`, cannot send a request a
second time and cannot skip the send. The retry and caching backends are
wrappers, and your own take a few lines.

## Writing a wrapper

There is one base class per execution model:
{py:class}`~action0.client.wrapper.SyncBackendWrapper`,
{py:class}`~action0.client.wrapper.AsyncBackendWrapper` and
{py:class}`~action0.client.wrapper.DeferredBackendWrapper`. Subclass the
one matching your backend, override `send` and reach the wrapped backend
through `self.inner`. The base class provides `inner`, `map` and
`repr()`, and it is itself a backend of that execution model — so the
static return types of {py:class}`~action0.client.client.Client` and
{py:class}`~action0.client.api.APIClient` stay exactly what they were.

The typical case is a step that has to be awaited before each request,
like a rate limiter. This one spaces requests at least `interval`
seconds apart:

```python
import asyncio
from collections.abc import Awaitable

from action0.client import AsyncBackendWrapper
from action0.client import Backend
from action0.client import Client
from action0.client.testing import AsyncStubBackend
from action0.req import Request
from action0.req import Response


class Throttled(AsyncBackendWrapper):
    """Send at most one request per ``interval`` seconds."""

    def __init__(self, inner: Backend[Awaitable[Response]], interval: float) -> None:
        super().__init__(inner)
        self._interval = interval
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def send(self, request: Request) -> Response:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = max(0.0, self._next_slot - now)
            self._next_slot = now + wait + self._interval
        await asyncio.sleep(wait)
        return await self.inner.send(request)


async def main() -> None:
    backend = Throttled(AsyncStubBackend(Response(200)), interval=0.05)
    client = Client(backend)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.gather(*(client.send(Request("https://api.example.com/")) for _ in range(3)))
    print(loop.time() - started >= 0.1)  # the third request waited two intervals
    # True
    print(backend)
    # Throttled(AsyncStubBackend(3 requests))


asyncio.run(main())
```

The synchronous and Twisted base classes work the same way: a
`SyncBackendWrapper.send` blocks (`time.sleep`, a `threading.Semaphore`)
and returns the response, a `DeferredBackendWrapper.send` returns a
`Deferred` chained in front of `self.inner.send(request)`.

A wrapper has no hooks of its own; they stay on the backend that does
the I/O.

## Stacking order

Wrappers nest, and the order decides what each layer sees. A request
travels from the outermost wrapper inwards. The recommended order is:

```text
cache  →  retry  →  throttle  →  backend (with its hooks)
```

```python
backend = CachingAsyncBackend(  # outermost: answers hits itself
    RetryingAsyncBackend(  # sends again on transient failures
        Throttled(  # passed before every attempt
            AsyncHttpxBackend(hooks=[MetricsHook()]),  # the I/O, instrumented
            interval=0.1,
        ),
        RetryPolicy(attempts=3),
    ),
    CachePolicy(ttl=60),
)
```

What follows from that order:

- **Hooks on the inner backend see every attempt.** A request retried
  twice runs `on_request` three times and reports three response times
  or errors. That is what you want from metrics: they describe what
  went over the wire.
- **A cache hit never reaches the inner backend.** Neither its hooks nor
  the retry and throttle layers are involved, so cached answers cost no
  rate-limit budget and do not show up in the metrics.
- **Only successful final responses are cached.** The cache sees what
  the retry layer returns, never the failed attempts in between.
- **A throttle below the retry is passed before every attempt**, so
  retries respect the rate limit too. Wrapped *around* the retry
  backend it would count requests instead of attempts — retries would
  then go out without waiting.

To count requests as your application sees them (one per call, cache
hits included), wrap the outermost backend in a small wrapper of your
own instead of using a hook on the inner one.
