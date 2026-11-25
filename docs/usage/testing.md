# Testing API clients

{py:mod}`action0.client.testing` answers requests in place of the
network. There are two ways to use it, and they share one API:

- **Mock the whole process** with a
  {py:class}`~action0.client.testing.MockRouter`: while it is active,
  every backend answers from the router's routes. The tests do not
  depend on which backend the application uses, and the backend does not
  have to be injected.
- **Inject a stub backend** into the code under test. A stub answers
  from a script of responses, and from the routes of its own router.

Either way hooks, error translation and the retrying and caching
backends run for real — only the I/O is replaced.

## Mocking the whole process

```python
from typing import Any

from action0.client import APIClient
from action0.client import JsonOperation
from action0.client import path_param
from action0.client.backends.urllib import UrllibBackend
from action0.client.testing import MockRouter


class GetItem(JsonOperation[Any]):
    path = "/items/{item_id}"
    item_id: int = path_param()


# the application's client, created wherever the application creates it
client = APIClient(UrllibBackend(), "https://api.example.com/v1")

with MockRouter() as mock:
    route = mock.get("https://api.example.com/v1/items/{item_id}").respond(
        200, json={"id": 7, "name": "Thing"}
    )
    print(client.send(GetItem(item_id=7)))
    # {'id': 7, 'name': 'Thing'}

print(route.call_count, route.calls.last.request)
# 1 Request(GET https://api.example.com/v1/items/7)
```

Replace `UrllibBackend` with the httpx, httpx2, aiohttp, requests,
urllib3, Twisted or thread-pool backend and the test does not change:
the router takes over inside the base class every backend is built on,
before anything library-specific happens. The library's own test suite
holds it to that — one module of tests, written against the router
only, runs unchanged against all thirteen shipped backends (stubs
included) and expects the same results.

With pytest there is nothing to set up. The package registers a plugin
that provides the router as the `action0_mock` fixture (pytest is not a
dependency of the library; the plugin module is only ever imported by
pytest):

```python
import pytest


def test_item_is_fetched(action0_mock):
    action0_mock.get("https://api.example.com/v1/items/7").respond(json={"id": 7})

    assert fetch_item(7).id == 7  # application code, whatever its backend


@pytest.mark.action0_mock(base_url="https://api.example.com/v1", assert_all_called=False)
def test_listing(action0_mock):
    action0_mock.get("/items").respond(json=[])  # relative to the base URL
    ...
```

### Routes

A route matches on what it is given; what is left out matches anything.
Routes are tried in the order they were added, and the first match
answers.

```python
mock.get("https://api.example.com/v1/items")  # method + full URL
mock.post("/v1/items", json={"name": "Thing"})  # any host; the JSON body must equal
mock.route(host="api.example.com", path__startswith="/v1/")  # any method
mock.get(path__regex=r"/v\d+/items/\d+")
mock.get("/v1/items/{item_id}")  # a template: each {name} is one path segment
mock.get("/v1/items", params={"page": "2"})  # these parameters, others allowed
mock.get("/v1/items", headers={"X-Tenant": "one"})  # these headers, others allowed
mock.post("/v1/upload", content=b"exact bytes")
mock.post("/v1/login", data={"user": "ada"})  # form data
mock.route()  # everything
```

The lookups are `scheme`, `host`, `port`, `path`, `path__startswith`,
`path__regex`, `params`, `headers`, `content`, `json` and `data`; a
misspelled one is refused when the route is defined.

### Answers

```python
from action0.client import TimeoutError
from action0.client import TransportError
from action0.req import Request
from action0.req import Response

route.respond(201, json={"id": 8}, headers={"Location": "/v1/items/8"})
route.respond(text="plain")  # also: content=, html=, chunks=
route.mock(return_value=Response(204))

# network failures: the library's own errors, raised like real ones
route.mock(side_effect=TimeoutError("too slow"))

# one answer per request, in order
route.mock(side_effect=[TransportError("reset"), Response(503), Response(200, body="ok")])


# computed from the request; may be async; gets the template's placeholders
def item(request: Request, item_id: str) -> Response:
    return Response(200, body=f'{{"id": {item_id}}}')


mock.get("/v1/items/{item_id}").mock(side_effect=item)

# let it through to the backend itself
mock.get("https://status.example.com/").pass_through()
```

A route without an answer responds with an empty `200`. A sequence that
is used up is an error, not a silent repeat.

### Assertions, and never reaching the network

```python
route.called, route.call_count
route.calls.last.request  # also .response, .exception
route.calls.assert_called_once()
mock.calls  # every request, in order
mock.assert_all_called()
mock.assert_all_mocked()
```

A request that no route matches raises an
{py:class}`~action0.client.testing.UnmockedRequestError` from `send()` —
an `AssertionError`, so a test fails instead of talking to a real
service. Leaving the `with` block (or the end of a test using the
fixture) checks again that no request went unmocked, because application
code may have swallowed the error, and that every route was called.
Turn the second check off with `MockRouter(assert_all_called=False)`
when routes are shared between tests.

The router covers every backend built on the library's base classes. For
requests that do not go through one — an HTTP library used directly, a
backend written from scratch — `MockRouter(block_network=True)`
additionally refuses every attempt to open a network connection while
the router is active, and reports it when the router is left.

### Body modes, chunks and broken downloads

A mocked response is handed over in the {doc}`body mode <streaming>` the
request asks for, exactly like a real one: preloaded bytes, a stream or
a spooled body. `chunks=` mocks a body that arrives in pieces — and an
exception among the chunks breaks it off at that point, which is how
code that resumes downloads is tested:

```python
from action0.client import BodyMode
from action0.client import RequestOptions
from action0.client import TransportError
from action0.client.backends.urllib import UrllibBackend
from action0.client.testing import MockRouter
from action0.req import Request

backend = UrllibBackend()
request = Request("https://api.example.com/feed")
RequestOptions(body=BodyMode.STREAM).apply(request)

with MockRouter() as mock:
    mock.get("https://api.example.com/feed").respond(
        chunks=[b"first, ", b"second", TransportError("connection lost")]
    )
    response = backend.send(request)  # status and headers are there
    received = []
    try:
        for chunk in response.body_producer().chunks():
            received.append(chunk)
    except TransportError as error:
        print("broke off:", error)
    # broke off: connection lost
print(received)
# [b'first, ', b'second']
```

For a preloaded or spooled request the same route makes `send()` itself
raise the error — as it would on the network.

### Why the switch is global

The router is activated for the whole process, not for the current
context. That is deliberate: application code crosses threads and event
loops — worker pools, `async_to_sync`-style bridges, a reactor thread —
and a context-local mock would silently not apply there, so the test
would talk to the real service. A global switch cannot miss.

It is also explicit. Only `MockRouter` (and the fixture, which is one)
turns it on; no environment variable or setting does, so it cannot be
active in production by accident. While no router is active, the switch
costs one module-global read and an `is None` test per request —
about 5 ns (measured on CPython 3.14: 3 to 6 ns, against roughly 180 ns
for a `send()` that does no I/O at all).

Tests that run in parallel threads of one process share the active
router; that is the price of the same property. Run such tests in
separate processes (as `pytest-xdist` does), or give each its own host
or path in the routes.

## Injecting a stub backend

One stub backend per execution model:
{py:class}`~action0.client.testing.StubBackend`,
{py:class}`~action0.client.testing.AsyncStubBackend`,
{py:class}`~action0.client.testing.DeferredStubBackend`. Canned (or
computed) responses in, requests recorded:

```python
from action0.client.testing import StubBackend
from action0.client.testing import deferred_result
from action0.req import Request
from action0.req import Response

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

An ordered script is fragile when requests are sent concurrently, or
when the code under test changes the order of its calls. Every stub
therefore has a router of its own, `backend.router`, with the full
route API. Its routes answer what they match; the script answers the
rest:

```python
from action0.client.testing import StubBackend
from action0.req import Request
from action0.req import Response

backend = StubBackend(Response(404))
health = backend.router.get("/health").respond(200, text="fine")

print(backend.send(Request("https://api.example.com/health")).body_str())
# fine
print(backend.send(Request("https://api.example.com/other")).status)
# 404
print(health.call_count, len(backend.requests))
# 1 2
```

A stub's router is private to that stub and never activated globally.
While a `MockRouter` *is* active, it answers the stubs as well — one way
of mocking for the whole test.

{py:func}`~action0.client.testing.deferred_result` extracts the result of
an already-fired `Deferred` (stubs and the router fire synchronously), so
Twisted code paths test without a reactor.

## Coming from respx

The route API follows [respx](https://lundberg.github.io/respx/), so
tests written for it move over mostly by renaming:

| respx | action0 |
|---|---|
| `respx.mock` / `respx_mock` fixture | `MockRouter()` / `action0_mock` fixture |
| `mock.get(url)`, `.post(...)`, `mock.route(...)` | the same |
| `route.respond(200, json=...)` | the same (`status` is the first argument) |
| `route.mock(return_value=..., side_effect=...)` | the same, with action0 `Response` objects |
| `httpx.Response(200, json=...)` | `Response(200, body=...)`, or `route.respond(json=...)` |
| `side_effect=httpx.ConnectError` | `side_effect=TransportError("...")` or `TimeoutError("...")` |
| `route.called`, `route.call_count`, `route.calls.last.request` | the same |
| `path__startswith`, `path__regex`, `params`, `headers`, `json`, `data`, `content` | the same |
| `url__regex`, `cookies`, `host__in` and other operator lookups | not available; use `path__regex` or a callable |
| `route.pass_through()` | the same |
| `assert_all_called`, `assert_all_mocked` | the same; with `assert_all_mocked=False` unmatched requests go to the backend instead of getting an automatic `200` |
| `call.request` is an `httpx.Request` | `call.request` is an action0 `Request` |

The difference that matters is underneath: respx patches httpx's
transport, so it mocks httpx. The router sits above the HTTP library and
works for every backend.
