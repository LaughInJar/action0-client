# Retries

Transient failures are common enough to deserve a battery: wrap any
backend in the retrying variant of its execution model
({py:class}`~action0.client.retry.RetryingSyncBackend` /
{py:class}`~action0.client.retry.RetryingAsyncBackend` /
{py:class}`~action0.client.retry.RetryingDeferredBackend`) and failed
sends are repeated with exponential backoff. The wrapper preserves the
execution model — the clients (and the type checker) treat it exactly
like the backend it wraps — and the wrapped backend's hooks run on every
attempt, so logs and metrics see the retries:

```python
from action0.client import APIClient
from action0.client import RetryingSyncBackend
from action0.client import RetryPolicy
from action0.client.backends.requests import RequestsBackend

with RequestsBackend() as inner:
    backend = RetryingSyncBackend(inner, RetryPolicy(attempts=5, backoff=0.2))
    client = APIClient(backend, "https://api.example.com/v1")
    item = client.send(GetItem(item_id=42))  # still typed Item
```

What is retried is the {py:class}`~action0.client.retry.RetryPolicy`'s
call — by default: `TransportError`s (which the backends translate all
network failures into) and the transient statuses 408/429/500/502/503/504,
for idempotent methods only (`methods=None` lifts that gate). When the
attempt budget is exhausted, the last response is returned (or the last
error raised) unchanged — the policy never invents failures.

The method gate exists because sending a `POST` twice may do something
twice. Some `POST` endpoints only read, though — a search or a
translation endpoint that takes its input in the body. Lifting the gate
for the whole client with `methods=None` would be too broad; instead a
single operation (or request) declares itself safe to retry with
{py:class}`~action0.client.options.RequestOptions`:

```python
from typing import Any

from action0.client import APIClient
from action0.client import JsonOperation
from action0.client import RequestOptions
from action0.client import RetryingSyncBackend
from action0.client import RetryPolicy
from action0.client import json_field
from action0.client.testing import StubBackend
from action0.req import Method
from action0.req import Response


class Translate(JsonOperation[Any]):
    method = Method.POST
    path = "/translate"
    request_options = RequestOptions(retry_safe=True)

    text: str = json_field()


inner = StubBackend(Response(503), Response(200, body='{"text": "hallo"}'))
backend = RetryingSyncBackend(inner, RetryPolicy(backoff=0))  # the default method gate
client = APIClient(backend, "https://api.example.com")
print(client.send(Translate(text="hello")))
# {'text': 'hallo'}
print(len(inner.requests))  # the POST was sent twice
# 2
```

`retry_safe=False` works the other way round and keeps a request from
being retried although its method would allow it. The attempt budget
and the list of retryable statuses and errors still apply either way.

## Retrying responses that cannot be parsed

A status check does not catch everything: a gateway that answers `200`
with an HTML page, or a JSON document that is cut off. By default such a
response fails the operation's `parse()` and the error is raised — it is
not retried, because the retrying backend only looks at the status.

Name the errors worth another attempt in the policy and the retry covers
the whole send-and-parse cycle:

```python
from typing import Any

from action0.client import APIClient
from action0.client import JsonOperation
from action0.client import PayloadError
from action0.client import RetryingSyncBackend
from action0.client import RetryPolicy
from action0.client.testing import StubBackend
from action0.req import Response


class GetRates(JsonOperation[Any]):
    path = "/rates"


inner = StubBackend(
    Response(200, body="<html>Service temporarily busy</html>"),
    Response(200, body='{"eur": 1.1'),  # cut off
    Response(200, body='{"eur": 1.1}'),
)
policy = RetryPolicy(attempts=3, backoff=0, retry_parse_errors=(PayloadError,))
client = APIClient(RetryingSyncBackend(inner, policy), "https://api.example.com")
print(client.send(GetRates()))
# {'eur': 1.1}
print(len(inner.requests))
# 3
```

{py:class}`~action0.client.errors.PayloadError` is what
{py:class}`~action0.client.operation.JsonOperation` raises for an empty
or malformed body. It is a subclass of `APIError`, but naming it does
not retry unexpected statuses — a 404 stays a plain `APIError` and is
raised at once. For failures of your own `load()` / `load_json()` name
the exception types they raise (`KeyError`, a validation error, ...).

How this fits together:

- There is one attempt counter. Transport errors, transient statuses and
  parse failures all draw on `attempts`; three attempts are at most
  three requests, whatever went wrong in between.
- The method gate applies as for every retry: a `POST` is only sent
  again if the policy allows it or the operation declares
  `retry_safe=True`.
- Each response is parsed once. The retrying backend and the client
  share the result.
- When the attempts run out, the last response is handed on unchanged:
  for a transient status `Operation.check` raises the `APIError` with
  that response attached, for an unparseable body the parse error of the
  last response is raised.
- A response that is thrown away is closed first, so a streamed or
  spooled body releases its connection or temporary file right away.
- This works in every execution model, also through
  {py:class}`~action0.client.backends.futures.ThreadPoolBackend`. It
  needs an {py:class}`~action0.client.api.APIClient`: a request sent
  through the raw `Client` has no parse step.

A {doc}`caching backend <caching>` uses the same knowledge: a response
the operation cannot parse is never stored.

## Retries never multiply

If retrying backends are stacked — say one configured by a library and
one added by the application — only the outermost one retries; the
inner ones pass its attempts through. Two default policies therefore
mean 3 requests, not 9, and the outermost policy is the one that
applies.

The HTTP libraries' own retry features are a different matter:
{py:class}`~action0.client.backends.urllib3.Urllib3Backend` passes
`retries=` on to urllib3, whose default retries a failed connection a
few times by itself — under a retrying backend each of *its* attempts
then includes urllib3's. Pass `retries=False` to leave retrying to one
place:

```python
backend = RetryingSyncBackend(Urllib3Backend(retries=False), RetryPolicy(attempts=3))
```

The same goes for a `requests` session with a retrying adapter or an
`httpx` transport created with `retries=`.

## Backoff

How long is waited is also the policy's call: exponential backoff with
"full jitter" — each wait is a uniformly random duration up to the
exponential delay, so a fleet of clients hitting the same outage does
not retry in lockstep (`jitter=False` waits the exact delays). A
`Retry-After` response header (seconds or HTTP-date form) overrides the
computed wait — the server knows best — capped at the policy's
`max_backoff`; `respect_retry_after=False` ignores it.

Two execution-model notes: the async wrapper waits with `asyncio.sleep`
by default — under trio pass `sleep=trio.sleep`; the Twisted wrapper
takes a `reactor=` for its backoff timer (the global reactor by
default).
