# Recipes

Small, self-contained patterns that come up in most real API clients.

## Proxies

The backends have no proxy parameters on purpose: proxying is
configuration of the *native* client, and every backend accepts one
(see {doc}`backends`):

```python
import httpx
from action0.client.backends.httpx import HttpxBackend

backend = HttpxBackend(httpx.Client(proxy="http://proxy.internal:3128"))
```

```python
import requests
from action0.client.backends.requests import RequestsBackend

session = requests.Session()
session.proxies.update({"https": "http://proxy.internal:3128"})
backend = RequestsBackend(session)
```

The counterparts: aiohttp takes `proxy=` per request or
`trust_env=True` on the session; urllib respects the `HTTP(S)_PROXY`
environment variables by default (or takes a
`urllib.request.ProxyHandler`); for urllib3 pass a
`urllib3.ProxyManager` as the pool.

## Pagination

An offset-paginated listing becomes a small generator around the page
operation — send, yield, bump the offset until the reported total is
reached:

```python
from typing import Iterator

from action0.client import SyncBackend


def iter_pokemon(client: PokeAPIClient[SyncBackend], page_size: int = 100) -> Iterator[str]:
    offset = 0
    while True:
        page = client.send(ListPokemon(limit=page_size, offset=offset))
        yield from page.names
        offset += page_size
        if offset >= page.count:
            return
```

Generators are lazy, so the loop stops requesting as soon as the caller
stops consuming (`itertools.islice` and friends). The runnable version —
operations, client and this loop against the real, public PokéAPI —
lives in
[examples/pokeapi.py](https://github.com/LaughInJar/action0-client/blob/main/examples/pokeapi.py).

## Metrics and tracing hooks

A {py:class}`~action0.client.hooks.Hook` sees every send — including
each retry attempt when the hook sits on the backend a retry wrapper
wraps — which is exactly what metrics and traces want. A counter/latency
hook is a few lines:

```python
from action0.client import Hook
from action0.req import Request, Response


class MetricsHook(Hook):
    """Feeds a histogram and an error counter (prometheus-client style)."""

    def on_response(self, request: Request, response: Response, elapsed: float) -> None:
        REQUEST_SECONDS.labels(request.method, str(response.status)).observe(elapsed)

    def on_error(self, request: Request, error: BaseException, elapsed: float) -> None:
        REQUEST_ERRORS.labels(request.method, type(error).__name__).inc()
```

For OpenTelemetry spans, the span opened in `on_request` must reach
`on_response`/`on_error`; carry it in `request.meta` (application
metadata that rides along with the request and never goes on the wire —
namespace your keys):

```python
from opentelemetry import trace
from action0.client import Hook
from action0.req import Request, Response

tracer = trace.get_tracer("my-service.http")


class TracingHook(Hook):
    """One client span per send attempt."""

    def on_request(self, request: Request) -> None:
        span = tracer.start_span(f"{request.method} {request.url.hostname}")
        span.set_attribute("http.request.method", request.method)
        span.set_attribute("url.full", request.url.as_str())
        request.meta["my-service.span"] = span

    def on_response(self, request: Request, response: Response, elapsed: float) -> None:
        span = request.meta.pop("my-service.span", None)
        if span is not None:
            span.set_attribute("http.response.status_code", response.status)
            span.end()

    def on_error(self, request: Request, error: BaseException, elapsed: float) -> None:
        span = request.meta.pop("my-service.span", None)
        if span is not None:
            span.record_exception(error)
            span.end()
```

## Correlation IDs

The same `request.meta` mechanism pairs a wire header with an
application-side identity — set once in
{py:meth}`~action0.client.api.APIClient.prepare` (or a hook), read it
anywhere the request resurfaces (hooks, error handlers, logs):

```python
import uuid
from action0.client import APIClient
from action0.req import Request


class MyAPIClient(APIClient):
    """Stamps every outgoing request with a correlation ID."""

    def prepare(self, request: Request) -> Request:
        request = super().prepare(request)  # merges the default headers
        correlation_id = str(uuid.uuid4())
        request.meta["my-service.correlation-id"] = correlation_id
        if "X-Correlation-ID" not in request.headers:
            request.headers.add("X-Correlation-ID", correlation_id)
        return request
```

A {py:class}`~action0.client.errors.TransportError` or
{py:class}`~action0.client.errors.APIError` carries `.request`, so the
correlation ID for the failure log is
`error.request.meta["my-service.correlation-id"]`.

## Response headers in typed results

Operations usually parse the body, but `load()` receives the whole
response — headers included. Pagination cursors, rate-limit budgets and
similar header-borne data belong in the result type:

```python
import json
from dataclasses import dataclass
from typing import Any
from action0.client import JsonOperation, query
from action0.req import Response


@dataclass
class Page:
    items: list[Any]
    next_cursor: str | None  # from a header, not the body


class ListItems(JsonOperation[Page]):
    path = "/items"
    cursor: "str | None" = query(default=None)

    def load(self, response: Response) -> Page:
        payload = json.loads(response.body_str() or "null")
        return Page(items=payload["items"], next_cursor=response.headers.get("X-Next-Cursor"))
```

(`load()` replaces the JSON decoding of
{py:class}`~action0.client.operation.JsonOperation` here; for body-only
results override `load_json()` instead, as in the operation examples
in {doc}`operations`.)
