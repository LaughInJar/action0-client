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
