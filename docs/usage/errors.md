# Error handling

Backends translate their library's exceptions into one family, so calling
code never depends on the HTTP library:

- {py:class}`~action0.client.errors.ClientError` — everything below
- {py:class}`~action0.client.errors.TransportError` — no usable HTTP
  response (DNS, connect, TLS, connection lost); the original library
  exception stays chained as `__cause__`
- {py:class}`~action0.client.errors.TimeoutError` — a `TransportError`
  that is also the built-in `TimeoutError`
- {py:class}`~action0.client.errors.APIError` — an HTTP response arrived
  but was unusable (unexpected status, malformed payload); carries
  `.request` and `.response`

```python
from action0.client import APIError
from action0.client import TransportError

try:
    item = client.send(GetItem(item_id=42))
except TransportError as error:
    ...  # network trouble — retry, circuit-break, ...
except APIError as error:
    ...  # the API answered, but not with what we expected
```

With async and Twisted backends the same errors arrive at `await` time /
in the errback instead of being raised synchronously.
