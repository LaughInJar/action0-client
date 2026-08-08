# Testing API clients

{py:mod}`action0.client.testing` ships one stub backend per execution
model: canned (or computed) responses in, requests recorded, hooks and
error translation running like on real backends —
{py:class}`~action0.client.testing.StubBackend`,
{py:class}`~action0.client.testing.AsyncStubBackend`,
{py:class}`~action0.client.testing.DeferredStubBackend`:

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

{py:func}`~action0.client.testing.deferred_result` extracts the result of
an already-fired `Deferred` (stubs fire synchronously), so Twisted code
paths test without a reactor.
