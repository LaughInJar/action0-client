# Instrumentation hooks

Backends built on the base classes run
{py:class}`~action0.client.hooks.Hook`s around every send — the same hook
API in all three execution models (hooks are plain synchronous calls that
run around the I/O, never inside it):

```python
import logging
import sys

from action0.client import LoggingHook
from action0.client.testing import StubBackend
from action0.req import Request
from action0.req import Response

logger = logging.getLogger("docs.hooks")
logger.propagate = False
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.StreamHandler(sys.stdout))

backend = StubBackend(Response(200), hooks=[LoggingHook(logger)])
backend.send(Request("https://api.example.com/health"))
# -> Request(GET https://api.example.com/health)
# <- Response(200 OK) for Request(GET https://api.example.com/health) in 0ms
```

`repr()` of requests and responses redacts secret header values and URL
passwords, which makes {py:class}`~action0.client.hooks.LoggingHook` safe
for production logs.

Custom hooks subclass {py:class}`~action0.client.hooks.Hook` and override
what they need — `on_request` (may replace the request), `on_response`
(may replace the response, gets the elapsed seconds) and `on_error`:

```python
from action0.client import Hook
from action0.req import Request
from action0.req import Response


class MetricsHook(Hook):
    """Collect response counts and timings per status."""

    def __init__(self) -> None:
        self.timings: dict[int, list[float]] = {}

    def on_response(self, request: Request, response: Response, elapsed: float) -> None:
        self.timings.setdefault(response.status, []).append(elapsed)


class DefaultUserAgentHook(Hook):
    """Stamp a User-Agent onto requests that have none."""

    def on_request(self, request: Request) -> Request | None:
        request.headers.setdefault("User-Agent", "my-service/1.0")
        return request
```

Hooks can be passed to any built-in backend (`hooks=[...]`) or appended
later (`backend.hooks.append(...)`).
