# Writing your own backend

A backend is anything implementing the
{py:class}`~action0.client.backend.Backend` protocol — two methods, no
registration:

- `send(request)` — do the I/O, return the (wrapped)
  {py:class}`~action0.req.response.Response`.
- `map(result, fn)` — apply `fn` inside the wrapper: plain call / await /
  `addCallback`. This is how generic code (like
  {py:meth}`APIClient.send <action0.client.api.APIClient.send>`) attaches
  response parsing without knowing your execution model.

In practice, subclass the matching base class and implement only the raw
I/O — hooks, error translation and `map` come for free:

```python
import urllib.error
import urllib.request

from action0.client import BaseSyncBackend
from action0.client import TransportError
from action0.req import Request
from action0.req import Response


class UrllibBackend(BaseSyncBackend):
    """A tiny stdlib-only backend."""

    def _send(self, request: Request) -> Response:
        raw = urllib.request.Request(
            request.url.as_str(),
            method=request.method,
            headers=dict(request.headers.items()),
            data=request.body_bytes(),
        )
        with urllib.request.urlopen(raw) as answer:
            return Response(
                answer.status,
                headers=answer.getheaders(),
                body=answer.read(),
                request=request,
            )

    def translate_error(self, error: Exception, request: Request) -> BaseException:
        if isinstance(error, urllib.error.URLError):
            return TransportError(str(error.reason), request=request)
        return error
```

`Client(UrllibBackend())` and `APIClient(UrllibBackend(), ...)` now work,
fully typed, hooks included. The async and Deferred base classes
({py:class}`~action0.client.backend.BaseAsyncBackend`,
{py:class}`~action0.client.backend.BaseDeferredBackend`) mirror this with
an `async def _send` / a Deferred-returning `_send`.

## Other execution models

The protocol is generic over the wrapper type, so backends are not limited
to the shipped execution models (sync, awaitable, Deferred, Future). Say
your framework has a result wrapper of its own:

```python
from typing import Callable
from typing import Generic
from typing import TypeVar

from action0.client import Client
from action0.req import Request
from action0.req import Response

T = TypeVar("T")
S = TypeVar("S")


class Box(Generic[T]):
    """Stand-in for your framework's own result wrapper."""


class BoxBackend:
    def send(self, request: Request) -> Box[Response]: ...
    def map(self, result: Box[T], fn: Callable[[T], S]) -> Box[S]: ...
```

`Client(BoxBackend()).send(request)` is a `Box[Response]` — the return
type is derived from the backend, no client code involved. For
{py:meth}`APIClient.send <action0.client.api.APIClient.send>` the parsed
results of such a backend are typed `Any` (rewriting "the wrapper, around
the operation's result type" for an *arbitrary* wrapper would need
higher-kinded types, which Python doesn't have — only the shipped
wrappers are spelled out as overloads); everything works normally at
runtime.

If you want that precision for your wrapper too, the escape hatch is to
subclass {py:class}`~action0.client.api.APIClient` and re-declare `send()`
with the wrapper spelled out — one `cast` is the price of the missing
higher-kinded types:

```python
from typing import TypeVar
from typing import cast

from action0.client import APIClient
from action0.client import Backend
from action0.client import Operation
from action0.req import Response

R = TypeVar("R")
BoxBackendT_co = TypeVar("BoxBackendT_co", bound=Backend[Box[Response]], covariant=True)


class BoxAPIClient(APIClient[BoxBackendT_co]):
    """An APIClient whose send() results are precisely typed Boxes."""

    # pyright ignore: its override check compares against every parent
    # overload without filtering by this subclass's self type (none of
    # the shipped-wrapper overloads can ever apply to a Box backend)
    def send(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, operation: Operation[R]
    ) -> Box[R]:
        return cast(Box[R], super().send(operation))
```

Now `BoxAPIClient(BoxBackend(), "https://api.example.com")` sends an
`Operation[Item]` as a `Box[Item]` — for every operation, without
per-call casts, and the concrete backend type stays visible on
`client.backend`. mypy and ty accept the override as-is; pyright wants the
one suppression shown above. The pattern is pinned in the typing test
suite (`tests/action0/client/test_typing.py`), so it keeps working.
