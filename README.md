# Action0-Client

[![CI](https://github.com/LaughInJar/action0-client/actions/workflows/ci.yml/badge.svg)](https://github.com/LaughInJar/action0-client/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/action0-client)](https://pypi.org/project/action0-client/)

Backend-agnostic, fully typed HTTP API clients: describe your API once —
as typed operations — and run it synchronously, on asyncio, on Twisted —
or on an execution model of your own — just by plugging in a different
backend. The type checker follows along:
the same `send()` returns a value, an `Awaitable` or a `Deferred`,
depending on the backend. Built on
[action0-req](https://github.com/LaughInJar/action0-req)
(request/response representation) and
[action0-url](https://github.com/LaughInJar/action0-url) (URL
representation).

The same typed operation, driven by three of the backends:

```python
client = APIClient(RequestsBackend(), "https://api.example.com/v1")
item = client.send(GetItem(item_id=42))  # Item

client = APIClient(AsyncHttpxBackend(), "https://api.example.com/v1")
item = await client.send(GetItem(item_id=42))  # Awaitable[Item]

client = APIClient(TwistedBackend(), "https://api.example.com/v1")
deferred = client.send(GetItem(item_id=42))  # Deferred[Item]
```

(`GetItem` is an ordinary typed operation class, written once — see
[Usage](#usage) for its definition.)

Eight backends are included —
[requests](https://requests.readthedocs.io/),
[httpx](https://www.python-httpx.org/) (sync and async),
[aiohttp](https://docs.aiohttp.org/),
[urllib3](https://urllib3.readthedocs.io/) and
[Twisted](https://twisted.org/), each behind an optional extra, plus
stdlib-only `urllib` and thread-pool backends — and the list is open: a
backend is one small structural protocol, so [writing your
own](https://laughinjar.github.io/action0-client/usage/custom-backends.html)
takes two methods, and it drives the same clients and operations.

Requires Python 3.11 or newer.

Full documentation including the API reference:
<https://laughinjar.github.io/action0-client/>

**Status:** the core API is complete — the backend protocol with the
eight backend implementations and instrumentation hooks, the raw
`Client`, the typed `Operation`/`JsonOperation`/`APIClient` layer, and the
stub backends for testing.

## Usage

### Raw requests, any execution model

A backend implements one structural protocol, `Backend[W]`, generic over
what its `send` wraps the response in (`W` is `Response`,
`Awaitable[Response]`, `Deferred[Response]`, `Future[Response]`, ...);
`Client` sends
[action0-req](https://github.com/LaughInJar/action0-req) `Request`s
through it, and the return type of `send()` follows the backend:

```python
from action0.client import Client
from action0.client.backends.requests import RequestsBackend
from action0.req import Request

with RequestsBackend() as backend:
    response = Client(backend).send(Request("https://example.com/"))  # Response
```

```python
from action0.client.backends.httpx import AsyncHttpxBackend

async with AsyncHttpxBackend() as backend:
    response = await Client(backend).send(Request("https://example.com/"))
```

```python
from action0.client.backends.twisted import TwistedBackend

deferred = Client(TwistedBackend()).send(Request("https://example.com/"))
deferred.addCallback(lambda response: print(response.status))  # Deferred[Response]
```

### Typed APIs: operations

Endpoints are dataclasses: HTTP method and path template are fixed on the
class, the variable parts are typed fields placed via specifiers
(`query`, `header`, `path_param`, `json_field`, `json_body`, `form_field`,
`body`), and
the generic parameter is the parsed result type:

```python
from dataclasses import dataclass
from typing import Any
from action0.client import APIClient, JsonOperation, path_param, query
from action0.req import Method


@dataclass
class Item:
    id: int
    name: str


class GetItem(JsonOperation[Item]):
    method = Method.GET
    path = "/items/{item_id}"

    item_id: int = path_param()
    expand: bool | None = query(default=None)  # None = not sent

    def load_json(self, data: Any) -> Item:
        return Item(id=data["id"], name=data["name"])
```

`APIClient` binds backend + base URL + default headers and runs the whole
pipeline — request building, send, status check, parsing — with the
result type following operation *and* backend (checked by mypy strict,
pyright and ty):

```python
client = APIClient(RequestsBackend(), "https://api.example.com/v1")
item = client.send(GetItem(item_id=42))  # Item

client = APIClient(AsyncHttpxBackend(), "https://api.example.com/v1")
item = await client.send(GetItem(item_id=42))  # Awaitable[Item]

client = APIClient(TwistedBackend(), "https://api.example.com/v1")
deferred = client.send(GetItem(item_id=42))  # Deferred[Item]
```

Transport problems surface uniformly as `TransportError`/`TimeoutError`,
API-level problems (unexpected status, malformed payload) as `APIError` —
regardless of the HTTP library underneath.

### Instrumentation and testing

Backends run `Hook`s (logging, metrics, tracing, request decoration)
around every send — the bundled `LoggingHook` logs redacted requests and
responses with timings. `action0.client.testing` ships recording stub
backends for all three execution models, so API clients are testable
without a server:

```python
from action0.client.testing import StubBackend
from action0.req import Response

backend = StubBackend(Response(200, body='{"id": 42, "name": "Thing"}'))
client = APIClient(backend, "https://api.example.com/v1")
assert client.send(GetItem(item_id=42)) == Item(id=42, name="Thing")
assert backend.requests[0].url.path == "/v1/items/42"
```

A complete example client (models, operations, auth, all three execution
models, runnable demo) lives in
[examples/petstore.py](examples/petstore.py);
[examples/pokeapi.py](examples/pokeapi.py) runs the same machinery —
plus pagination and a cached, retrying backend — against the real,
public [PokéAPI](https://pokeapi.co).

## Installation

Install from [PyPI](https://pypi.org/project/action0-client/). The HTTP
libraries are optional extras — pick what you need (`requests`, `httpx`,
`aiohttp`, `urllib3`, `twisted`, `all`); the stdlib `urllib` and thread-pool backends
work without any extra:

```shell
uv add "action0-client[httpx]"
```

## Development

The project is managed with [uv](https://docs.astral.sh/uv/); `uv run`
creates and syncs the virtual environment automatically (the dev group
includes all backend libraries):

```shell
uv run pytest        # run the tests (incl. the docstring examples as doctests)
uv run ruff check    # lint
uv run ruff format   # format
uv run mypy          # type-check (also: uv run pyright, uv run ty check)

# build the docs (Sphinx; deployed to GitHub Pages on push to main)
uv run --group docs sphinx-build -W docs docs/_build/html
```

### Releasing

The version lives only in `src/action0/client/__init__.py`
(`__version__`). To release: bump it, merge to `main`, then tag the
release commit and push the tag — the release workflow re-runs all
checks, verifies the tag matches `__version__`, builds sdist + wheel and
publishes to PyPI via trusted publishing:

```shell
git tag v0.1.0
git push origin v0.1.0
```

## AI-assisted development

In the spirit of transparency: most of this project's code, tests and
documentation are written by [Claude Code](https://claude.com/claude-code),
Anthropic's coding agent — under human direction and review. The designs
are specified, discussed and iterated by a human, and every change is
reviewed before it lands in `main` or in a release. AI-authored commits
carry a `Co-Authored-By: Claude ...` trailer.

## About action0

This is just the namespace I like to use for my personal projects.
I quite like namespaces.
