"""
The generic API client (:py:class:`APIClient`): binds a backend, a base URL
and default headers, and sends typed
:py:class:`~action0.client.operation.Operation` instances.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Awaitable
from typing import Generic
from typing import TypeVar
from typing import cast
from typing import overload

from action0.req import Headers
from action0.req import Request
from action0.req.headers import HeaderTypes
from action0.url import Url

from .backend import AsyncBackend
from .backend import DeferredBackend
from .backend import SyncBackend
from .client import BackendT_co
from .operation import Operation

if TYPE_CHECKING:
    # twisted is an optional dependency: only the type checker sees this
    from twisted.internet.defer import Deferred

R = TypeVar("R")
"""The parsed result type of the operation being sent."""


class APIClient(Generic[BackendT_co]):
    """
    A client for one HTTP API: it holds the backend, the base URL and the
    default headers, and :py:meth:`send` turns an
    :py:class:`~action0.client.operation.Operation` into a request, sends
    it and parses the response — through :py:meth:`Operation.parse
    <action0.client.operation.Operation.parse>`, attached via the backend's
    ``map``.

    The result type follows the operation *and* the backend: for an
    ``Operation[Item]``, ``send`` returns

    - ``Item`` with a sync backend,
    - ``Awaitable[Item]`` with an async backend,
    - ``Deferred[Item]`` with a Twisted backend

    — and the type checker knows it. The same client class serves all
    three execution models; only the backend changes.

    Example (with the test-double backend standing in for a real one)::

        >>> from typing import Any
        >>> from action0.client import JsonOperation, query
        >>> from action0.client.testing import StubBackend
        >>> from action0.req import Response
        >>>
        >>> class SearchItems(JsonOperation[Any]):
        ...     path = "/items"
        ...     q: str = query()
        >>>
        >>> backend = StubBackend(Response(200, body='{"hits": 2}'))
        >>> client = APIClient(backend, "https://api.example.com/v1")
        >>> client.send(SearchItems(q="thing"))
        {'hits': 2}
        >>> backend.requests[0].url.as_str()
        'https://api.example.com/v1/items?q=thing'

    The same operations sent asynchronously — only the backend differs::

        >>> import asyncio
        >>> from action0.client.testing import AsyncStubBackend
        >>>
        >>> client = APIClient(AsyncStubBackend(Response(200, body="[]")), "https://api.example.com/v1")
        >>> asyncio.run(client.send(SearchItems(q="thing")))
        []

    Real API clients usually subclass, fixing base URL and auth (keep the
    backend type variable so the typed overloads keep working)::

        class ExampleClient(APIClient[BackendT_co]):
            def __init__(self, backend: BackendT_co, token: str) -> None:
                super().__init__(
                    backend,
                    "https://api.example.com/v1",
                    headers={"Authorization": f"Bearer {token}"},
                )
    """

    def __init__(
        self,
        backend: BackendT_co,
        base_url: str | Url,
        *,
        headers: HeaderTypes | None = None,
    ) -> None:
        """
        :param backend: the backend performing the HTTP I/O — an
                        implementation of
                        :py:class:`~action0.client.backend.SyncBackend`,
                        :py:class:`~action0.client.backend.AsyncBackend` or
                        :py:class:`~action0.client.backend.DeferredBackend`
        :param base_url: the URL the operations' paths are appended to,
                         e.g. ``"https://api.example.com/v2"`` (a given
                         ``Url`` instance is copied)
        :param headers: default header lines added to every request that
                        does not set them itself — the typical place for
                        ``Authorization`` and friends
        """
        self._backend = backend
        self.base_url = Url(base_url) if isinstance(base_url, str) else base_url.copy()
        """The URL every operation path is appended to."""
        self.headers = Headers(headers)
        """The default headers, added to requests that don't set them."""

    @property
    def backend(self) -> BackendT_co:
        """The backend this client sends through (as its concrete type)."""
        return self._backend

    def prepare(self, request: Request) -> Request:
        """
        Last touches before a request is sent: the default
        :py:attr:`headers` are added — per header field, only if the
        request does not set that field itself.

        Override this for dynamic per-request work like signing or
        token refresh (call ``super().prepare(request)`` to keep the
        default-header behavior)::

            class SignedClient(APIClient[BackendT_co]):
                def prepare(self, request: Request) -> Request:
                    request = super().prepare(request)
                    request.headers["X-Signature"] = self._sign(request)
                    return request

        :param request: the request built from an operation
        :return: the request to actually send
        """
        for name in self.headers:
            if name not in request.headers:
                request.headers.add(name, self.headers.get_all(name))
        return request

    @overload
    def send(self: APIClient[SyncBackend], operation: Operation[R]) -> R: ...

    @overload
    def send(self: APIClient[DeferredBackend], operation: Operation[R]) -> Deferred[R]: ...

    @overload
    def send(self: APIClient[AsyncBackend], operation: Operation[R]) -> Awaitable[R]: ...

    def send(self, operation: Operation[Any]) -> Any:
        """
        Send an operation: build its request (:py:meth:`Operation.as_request
        <action0.client.operation.Operation.as_request>` with this client's
        base URL, then :py:meth:`prepare`), send it through the backend,
        and parse the response via :py:meth:`Operation.parse
        <action0.client.operation.Operation.parse>` — attached with the
        backend's ``map``, so it runs inside whatever wrapper the backend
        returns.

        :param operation: the operation to execute
        :return: the parsed result, wrapped according to the backend's
                 execution model: plain for a sync backend, awaitable for
                 an async backend, a Deferred for a Twisted backend
        :raises action0.client.errors.ClientError: transport failures and
                response parsing failures (for async and Twisted backends
                they arrive at ``await`` time / in the errback instead of
                being raised here)
        """
        request = self.prepare(operation.as_request(self.base_url))
        # all three backend protocols share the same send/map call shape;
        # the checker cannot express "same wrapper in and out" across the
        # union, so the implementation picks one protocol pro forma — the
        # overloads above give callers the precise types
        backend = cast("SyncBackend", self._backend)
        return backend.map(backend.send(request), operation.parse)

    def __repr__(self) -> str:
        """
        :return: the client with its base URL and backend, e.g.
                 ``APIClient(https://api.example.com/v1 via StubBackend())``
        """
        return f"{self.__class__.__name__}({self.base_url.as_str()} via {self._backend!r})"
