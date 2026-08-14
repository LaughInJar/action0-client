# Typed API clients: operations

An {py:class}`~action0.client.operation.Operation` describes one endpoint.
The constant parts (HTTP method, path template) are class attributes; the
variable parts are typed dataclass fields, placed into the request by the
specifiers from {py:mod}`action0.client.fields`; the generic parameter is
the parsed result type:

```python
from dataclasses import dataclass
from typing import Any

from action0.client import JsonOperation
from action0.client import path_param
from action0.client import query
from action0.req import Method


@dataclass
class Item:
    id: int
    name: str


class GetItem(JsonOperation[Item]):
    method = Method.GET
    path = "/items/{item_id}"

    item_id: int = path_param()  # fills the {item_id} placeholder
    expand: bool | None = query(default=None)  # ?expand=...; None = not sent

    def load_json(self, data: Any) -> Item:
        return Item(id=data["id"], name=data["name"])
```

Subclasses become keyword-only dataclasses automatically (no decorator
needed), so `GetItem(item_id=42)` is type-checked, has a useful `repr()`
and compares by value. The class definition is validated eagerly: a path
placeholder without a matching `path_param()` field (or vice versa), a
field named like a class attribute, or conflicting body declarations all
raise `TypeError` at import time.

{py:class}`~action0.client.api.APIClient` binds the backend, the base URL
and default headers, and `send()` runs the full pipeline — build the
request, send it, `parse()` the response:

```python
from action0.client import APIClient
from action0.client.testing import StubBackend
from action0.req import Response

backend = StubBackend(Response(200, body='{"id": 42, "name": "Thing"}'))
client = APIClient(backend, "https://api.example.com/v1")

print(client.send(GetItem(item_id=42, expand=True)))
# Item(id=42, name='Thing')
print(backend.requests[0].url.as_str())
# https://api.example.com/v1/items/42?expand=true
print(backend.requests[0].headers["Accept"])
# application/json
```

And, the whole point: with an async or Twisted backend **the same
operations** yield `Awaitable[Item]` / `Deferred[Item]`:

```python
from action0.client.testing import AsyncStubBackend
from action0.client.testing import DeferredStubBackend

async_client = APIClient(AsyncStubBackend(...), "https://api.example.com/v1")
item = await async_client.send(GetItem(item_id=42))  # Awaitable[Item]

twisted_client = APIClient(DeferredStubBackend(...), "https://api.example.com/v1")
deferred = twisted_client.send(GetItem(item_id=42))  # Deferred[Item]
deferred.addCallback(...)
```

## Field placement

```python
from typing import Any

from action0.client import JsonOperation
from action0.client import body
from action0.client import form_field
from action0.client import header
from action0.client import json_body
from action0.client import json_field
from action0.client import path_param
from action0.client import query
from action0.req import Method


class CreateItem(JsonOperation[Any]):
    method = Method.POST
    path = "/shelves/{shelf}/items"

    shelf: str = path_param()  # path template placeholder
    dry_run: bool | None = query("dryRun", default=None)  # renamed on the wire
    locale: str | None = header("Accept-Language", default=None)
    token: str = header("X-API-Key", repr=False)  # kept out of repr()

    name: str = json_field()  # keys of the JSON body object
    tags: list[str] | None = json_field(default=None)
```

- All `json_field()`s together form the JSON object body (with
  `Content-Type: application/json` added if unset).
- `json_body()` sends one field — scalar, mapping, sequence, dataclass —
  as the entire JSON body instead.
- All `form_field()`s together form an
  `application/x-www-form-urlencoded` body — the classic HTML form POST
  and the shape of OAuth token endpoints. Values serialize exactly like
  query parameters:

  ```python
  class RequestToken(JsonOperation[Any]):
      method = Method.POST
      path = "/oauth/token"

      grant_type: str = form_field(default="client_credentials")
      client_id: str = form_field()
      client_secret: str = form_field(repr=False)


  # body: grant_type=client_credentials&client_id=...&client_secret=...
  # Content-Type: application/x-www-form-urlencoded
  ```

- `body()` sends one field as the raw body: `bytes`, `str` or a streaming
  {py:class}`~action0.req.body.BodyProducer`.
- Only one of these body forms per operation (several `json_field()`s
  *or* several `form_field()`s *or* a single `json_body()` / `body()`),
  checked at class-creation time.

Serialization is uniform and overridable: `None` means "not sent" (except
for path parameters, which must not be `None`), enums send their
``value``, dates/datetimes their ISO form, booleans the web-style
``true``/``false``, and list values repeat the query parameter or header.
A `serialize=` callable on any field overrides the value's serialization;
`serialize_value` / `serialize_json_value` on the operation override it
family-wide.

Fields *without* a specifier follow the operation's `default_location` —
query parameters by default. An API family whose endpoints all POST JSON
can flip that once in a base class:

```python
from typing import Any
from typing import ClassVar

from action0.client import JsonOperation
from action0.client.fields import Location
from action0.req import Method


class RpcOperation(JsonOperation[Any]):
    """Every endpoint of this API takes a JSON object body."""

    method = Method.POST
    default_location: ClassVar[Location] = Location.JSON_FIELD


class SearchProducts(RpcOperation):
    path = "/search_product"
    query: str  # plain fields → JSON body keys, thanks to the base
    limit: int = 30
```

## Response handling

`parse()` = `check()` + `load()`:

- `check()` raises {py:class}`~action0.client.errors.APIError` for
  anything but 2xx — override it for per-endpoint status policies (e.g.
  tolerate 404 and return `None`).
- `load()` turns the vetted response into the result.
  {py:class}`~action0.client.operation.JsonOperation` implements it by
  decoding JSON and delegating to `load_json(data)`; override `load_json`
  for typed models (the default returns the decoded payload as-is, which
  fits `JsonOperation[Any]`).

For non-JSON endpoints subclass
{py:class}`~action0.client.operation.Operation` directly and implement
`load()` — see `DeletePet` in
[examples/petstore.py](https://github.com/LaughInJar/action0-client/blob/main/examples/petstore.py)
for a 204-no-body endpoint.

## A real client class

Applications usually wrap `APIClient` once per API, fixing base URL and
auth. Keep the backend type variable, so the typed overloads keep working
for every execution model:

```python
from action0.client import APIClient
from action0.client import BackendT_co


class PetStoreClient(APIClient[BackendT_co]):
    def __init__(self, backend: BackendT_co, token: str) -> None:
        super().__init__(
            backend,
            "https://petstore.example.com/v1",
            headers={"Authorization": f"Bearer {token}"},
        )
```

Default headers fill gaps only — a header the operation sets itself wins.
For dynamic per-request work (signing, token refresh) override
{py:meth}`~action0.client.api.APIClient.prepare`.

The complete, runnable version of this client — models, operations,
client, all three execution models and the stub-backed demo — lives in
[examples/petstore.py](https://github.com/LaughInJar/action0-client/blob/main/examples/petstore.py).
For the same machinery against a real, public API — including pagination
and a cached, retrying backend — see
[examples/pokeapi.py](https://github.com/LaughInJar/action0-client/blob/main/examples/pokeapi.py).
