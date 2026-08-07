# API reference

Everything public is importable from the package root:

```python
from action0.client import Client, APIClient
from action0.client import Operation, JsonOperation
from action0.client import body, header, json_body, json_field, path_param, query
from action0.client import Backend, SyncBackend, AsyncBackend, DeferredBackend, FuturesBackend
from action0.client import BackendT_co, SendResultT_co
from action0.client import BaseSyncBackend, BaseAsyncBackend, BaseDeferredBackend
from action0.client import Hook, LoggingHook
from action0.client import (
    RetryPolicy,
    RetryingSyncBackend,
    RetryingAsyncBackend,
    RetryingDeferredBackend,
)
from action0.client import ClientError, TransportError, TimeoutError, APIError
```

The backend implementations and the test doubles are imported from their
modules, so the optional HTTP libraries are only touched when actually
used:

```python
from action0.client.backends.requests import RequestsBackend
from action0.client.backends.httpx import HttpxBackend, AsyncHttpxBackend
from action0.client.backends.aiohttp import AiohttpBackend
from action0.client.backends.twisted import TwistedBackend
from action0.client.backends.urllib import UrllibBackend
from action0.client.backends.urllib3 import Urllib3Backend
from action0.client.backends.futures import ThreadPoolBackend
from action0.client.testing import StubBackend, AsyncStubBackend, DeferredStubBackend
```

## Client

```{eval-rst}
.. automodule:: action0.client.client
   :members:
   :special-members: __repr__
```

## Backend protocol and base classes

```{eval-rst}
.. automodule:: action0.client.backend
   :members:
   :private-members: _send
```

## Operations

```{eval-rst}
.. automodule:: action0.client.operation
   :members:
```

## Field specifiers

```{eval-rst}
.. automodule:: action0.client.fields
   :members:
```

## APIClient

```{eval-rst}
.. automodule:: action0.client.api
   :members:
   :special-members: __repr__
```

## Hooks

```{eval-rst}
.. automodule:: action0.client.hooks
   :members:
```

## Retries

```{eval-rst}
.. automodule:: action0.client.retry
   :members:
```

## Errors

```{eval-rst}
.. automodule:: action0.client.errors
   :members:
```

## Built-in backends

```{eval-rst}
.. automodule:: action0.client.backends
```

```{eval-rst}
.. automodule:: action0.client.backends.requests
   :members:
```

```{eval-rst}
.. automodule:: action0.client.backends.httpx
   :members:
```

```{eval-rst}
.. automodule:: action0.client.backends.aiohttp
   :members:
```

```{eval-rst}
.. automodule:: action0.client.backends.urllib
   :members:
```

```{eval-rst}
.. automodule:: action0.client.backends.urllib3
   :members:
```

```{eval-rst}
.. automodule:: action0.client.backends.futures
   :members:
```

```{eval-rst}
.. automodule:: action0.client.backends.twisted
   :members:
```

## Testing utilities

```{eval-rst}
.. automodule:: action0.client.testing
   :members:
```
